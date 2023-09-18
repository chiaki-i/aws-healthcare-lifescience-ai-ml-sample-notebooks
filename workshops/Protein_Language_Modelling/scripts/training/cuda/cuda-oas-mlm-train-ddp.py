# Copyright 2019-2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

# model_checkpoint="facebook/esm2_t48_15B_UR50D" # 15B params
# model_checkpoint="facebook/esm2_t36_3B_UR50D"
# model_checkpoint="facebook/esm2_t33_650M_UR50D"
# model_checkpoint="facebook/esm2_t30_150M_UR50D"
# model_checkpoint="facebook/esm2_t12_35M_UR50D"
# model_checkpoint = "facebook/esm2_t6_8M_UR50D"  # 8M params

# torchrun train.py --train_sample_count=50000 --model_id="facebook/esm2_t33_650M_UR50D" --num_epochs=3

import os
import argparse
import copy
from datasets import load_from_disk, load_dataset, DatasetDict
import math
from timeit import default_timer as timer
import torch
from torch.optim import AdamW
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from tqdm.auto import tqdm
import json
from transformers import (
    AutoTokenizer,
    EsmForMaskedLM,
    DataCollatorForLanguageModeling,
    set_seed,
    get_scheduler,
    SchedulerType,
)
from transformers.models.esm.configuration_esm import get_default_vocab_list

### 0. Import Torch Distributed Training
import torch.distributed as dist


def parse_args():
    """Parse the arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--lr", type=float, default=5e-5, help="Learning rate to use for training."
    )
    parser.add_argument(
        "--lr_scheduler_type",
        type=SchedulerType,
        default="linear",
        help="The scheduler type to use.",
        choices=[
            "linear",
            "cosine",
            "cosine_with_restarts",
            "polynomial",
            "constant",
            "constant_with_warmup",
        ],
    )
    parser.add_argument(
        "--max_length",
        type=int,
        default=142,
        help="Max length of sequence for collator.",
    )
    parser.add_argument(
        "--model_dir",
        type=str,
        default=os.environ["SM_MODEL_DIR"],
        help="Path to model output folder.",
    )
    parser.add_argument(
        "--model_id",
        type=str,
        default="facebook/esm2_t33_650M_UR50D",
        help="Model id to use for training.",
    )
    parser.add_argument(
        "--num_epochs", type=int, default=1, help="Number of epochs to train."
    )
    parser.add_argument(
        "--num_warmup_steps",
        type=int,
        default=0,
        help="Number of steps for the warmup in the lr scheduler.",
    )
    parser.add_argument(
        "--per_device_eval_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the evaluation dataloader.",
    )
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed.",
    )
    parser.add_argument(
        "--training_dir",
        type=str,
        default=os.environ["SM_CHANNEL_TRAIN"],
        help="Path to train dataset.",
    )
    parser.add_argument(
        "--test_dir",
        type=str,
        default=os.environ["SM_CHANNEL_TEST"],
        help="Path to evaluation dataset.",
    )
    parser.add_argument(
        "--logging_steps",
        type=int,
        default=10,
        help="Number of steps between logging updates.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of steps between gradient optimization.",
    )
    parser.add_argument(
        "--train_sample_count",
        type=int,
        default=None,
        help="Number of training samples to pre-process.",
    )
    parser.add_argument(
        "--steps_this_run",
        type=int,
        default=None,
        help="Max number of steps.",
    )
    parser.add_argument(
        "--pretrain",
        type=int,
        default=0,
        help="Initialize random weights?",
    )

    ########################## Step 1 : DDP related arguements ###########################
    parser.add_argument('--hosts', type=list, default=json.loads(os.environ['SM_HOSTS']))
    parser.add_argument('--current-host', type=str, default=os.environ['SM_CURRENT_HOST'])
    parser.add_argument('--num-gpus', type=int, default=os.environ['SM_NUM_GPUS'])
    ########################## DDP related ###########################    

    args, _ = parser.parse_known_args()
    return args


def calc_perplexity(loss):
    try:
        perplexity = math.exp(loss)
    except OverflowError:
        perplexity = float("inf")
    return perplexity


def report_metrics(
    rank, start_time, loss, epoch, steps, sample_count, token_count, prefix=None
):
    reported_loss = loss.detach().float()
    now = timer()
    duration = now - start_time
    samples_per_sec = sample_count / duration
    tokens_per_sec = token_count / duration
    perplexity = calc_perplexity(reported_loss)
    if prefix:
        prefix = prefix + " "
    if rank == 0:
        print(
            f"Epoch: {epoch}, Step: {steps}, {prefix}Loss: {reported_loss:0.4f}, {prefix}Perplexity: {perplexity:0.4f}, {prefix}Samples/sec: {samples_per_sec:0.4f}, {prefix}Tokens/sec: {tokens_per_sec:0.4f}"
        )

    return None


def worker(local_rank, args):
    
    run_start = timer()

    # Step 4: Compute the global rank (global_rank) of the spawned process as:
    # =node_id*num_gpus + local_rank.
    # To properly initialize and synchornize each process, 
    # invoke dist.init_process_group with the approrpriate parameters:
    # backend='nccl', world_size=WORLD_SIZE, rank=global_rank

    world_size = len(args.hosts) * args.num_gpus
    
    #ToDO : check below
    os.environ['WORLD_SIZE'] = str(world_size)
    print("[DDP] World Size is [{}]".format(world_size))

    node_id = args.hosts.index(args.current_host)
    global_rank = node_id * args.num_gpus + local_rank
    os.environ['RANK'] = str(global_rank)
    
    dist.init_process_group(backend="nccl", world_size=world_size, rank=global_rank)

    print("[DDP] Initialized the distributed environment: {} backend on {} nodes. ".format(
            'nccl', dist.get_world_size()) + 'Current host rank is {}. Number of gpus: {}'.format(
            dist.get_rank(), args.num_gpus))   


    if args.seed is not None:
        set_seed(args.seed)
        torch.manual_seed(args.seed)
        torch.cuda.manual_seed(args.seed)


    # DDP Step 5: Download the data only once per host, however then running on SageMaker data already downloaded

    # data_src = "bloyal/oas_paired_human_sars_cov_2"
    # train_sample_count = args.train_sample_count
    # test_sample_count = int(train_sample_count * 0.2)

    # if local_rank == 0:
    #     print("[DDP] Started Downloading data in host [{}] rank [{}]".format(args.current_host, local_rank))
    #     train_dataset = load_dataset(path=args.training_dir, split=f"train[:{train_sample_count}]", download_mode="force_redownload")
    #     test_dataset = load_dataset(path=args.test_dir, split=f"test[:{test_sample_count}]", download_mode="force_redownload")
    #     print("[DDP] Finished Downloading data in host [{}] rank [{}]".format(args.current_host, local_rank))
    # dist.barrier()
    
    # if local_rank != 0:
    #     print("[DDP] Reusing Downloaded data in host [{}] rank [{}]".format(args.current_host, local_rank))
    #     train_dataset = load_dataset(data_src, split=f"train[:{train_sample_count}]", download_mode="reuse_dataset_if_exists")
    #     test_dataset = load_dataset(data_src, split=f"test[:{test_sample_count}]", download_mode="reuse_dataset_if_exists")
    #     print("[DDP] Reused Downloaded data in host [{}] rank [{}]".format(args.current_host, local_rank))

    train_dataset = load_from_disk(args.training_dir)
    test_dataset = load_from_disk(args.test_dir)
    
    # DDP Step 6: Wrap training and validation data with DistributedSampler, and enable data shuffling
    train_sampler = torch.utils.data.distributed.DistributedSampler(
        train_dataset,
        num_replicas=world_size,
        rank=global_rank,
        shuffle=True
    )
    
    test_sampler = torch.utils.data.distributed.DistributedSampler(
        test_dataset,
        num_replicas=world_size,
        rank=global_rank,
        shuffle=True
    )

    print("[DDP] Train and test samplers set in host [{}] rank [{}]".format(args.current_host, local_rank))

    tokenizer = AutoTokenizer.from_pretrained(args.model_id)
    tokenizer.model_max_length = args.max_length
    data_collator = DataCollatorForLanguageModeling(
        tokenizer=tokenizer, mlm_probability=0.15
    )
    train_loader = DataLoader(
        train_dataset,
        collate_fn=data_collator,
        batch_size=args.per_device_train_batch_size,
        sampler=train_sampler,
        shuffle=False if train_sampler else True,
    )
    eval_loader = DataLoader(
        test_dataset,
        collate_fn=data_collator,
        batch_size=args.per_device_eval_batch_size,
        sampler=test_sampler,
        shuffle=False if test_sampler else True,
    )



    # 
    # test_sample_count = int(train_sample_count * 0.2)
    # train_dataset = load_dataset(src, split=f"train[:{train_sample_count}]")
    # test_dataset = load_dataset(src, split=f"test[:{test_sample_count}]")
    # dataset = DatasetDict({"train": train_dataset, "test": test_dataset}).rename_column(
    #     "sequence_alignment_aa_heavy", "text"
    # )
    # tokenizer = AutoTokenizer.from_pretrained("facebook/esm2_t6_8M_UR50D")
    # sequence_length = 142
    # encoded_dataset = dataset.map(
    #     tokenize_data,
    #     batched=True,
    #     num_proc=os.cpu_count(),
    #     remove_columns=dataset["train"].column_names,
    #     fn_kwargs={
    #         "tokenizer": tokenizer,
    #         "sequence_length": sequence_length,
    #     },
    # )

    # encoded_dataset.set_format("torch", columns=["input_ids", "attention_mask"])
    # return encoded_dataset

    # DDP Step 7: Modify the torch.device call from "cuda:0" to "cuda:<local_rank>" 
    # to pin the process to its assigned GPU. 
    device = torch.device("cuda:" + str(local_rank) if torch.cuda.is_available() else "cpu")
    print("[DDP] Device identified in host [{}] rank [{}] as [{}]".format(args.current_host, local_rank, device))

    ## Load model
    model = EsmForMaskedLM.from_pretrained(args.model_id)
    if args.pretrain:
        my_config = copy.deepcopy(model.config)
        my_config.vocab_list = get_default_vocab_list()
        my_config.vocab_size = len(my_config.vocab_list)
        model = EsmForMaskedLM(my_config)

    model.to(device)
    model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[local_rank], find_unused_parameters=True)
    print("[DDP] Model loaded in host [{}] rank [{}]".format(args.current_host, local_rank))

    # Define training metrics
    num_update_steps_per_epoch = len(train_loader)
    num_total_training_steps = args.num_epochs * num_update_steps_per_epoch
    total_train_batch_size = args.per_device_train_batch_size * world_size
    samples_processed_per_logging_update = total_train_batch_size * args.logging_steps
    tokens_processed_per_logging_update = (
        samples_processed_per_logging_update * args.max_length
    )

    # Define eval metrics
    num_eval_steps_per_epoch = len(eval_loader)
    total_eval_batch_size = args.per_device_eval_batch_size * world_size
    samples_processed_per_eval = total_eval_batch_size * num_eval_steps_per_epoch
    tokens_processed_per_eval = samples_processed_per_eval * args.max_length

    optimizer = AdamW(model.parameters(), args.lr)

    lr_scheduler = get_scheduler(
        name=args.lr_scheduler_type,
        optimizer=optimizer,
        num_warmup_steps=args.num_warmup_steps,
        num_training_steps=num_total_training_steps,
    )


    if global_rank == 0:
        print("***** Running training *****")
        print(f"\nNum examples: {len(train_dataset)}")
        print(f"\nNum Epochs: {args.num_epochs}")
        print(
            f"\nInstantaneous batch size per device = {args.per_device_train_batch_size}"
        )
        print(
            f"\nTotal train batch size (w. parallel, distributed & accumulation) = {total_train_batch_size}"
        )
        print(f"\nTotal optimization steps = {num_total_training_steps}")

    # Only show the progress bar once on each machine.
    progress_bar = tqdm(
        range(num_total_training_steps), disable=not local_rank == 0, miniters=1
    )
    completed_steps = 0
    starting_epoch = 0

    # Start training loop
    for epoch in range(starting_epoch, args.num_epochs):
        if local_rank == 0:
            print("######################### Train #########################")

        train_sampler.set_epoch(epoch)

        model.train()
        for idx, batch in enumerate(train_loader):
            train_loop_start_time = timer()
            progress_bar.update(1)
            batch = {
                k: v.to(device) for k, v, in batch.items()
            }  # Transfer data to accelerator
            outputs = model(**batch)  # Forward pass
            optimizer.zero_grad()  # Set all tensor gradients to zero
            loss = outputs.loss  # Calculate loss
            loss.backward()  # Calculate new gradients with backprop
            lr_scheduler.step()  # Update scheduler

            if ((idx + 1) % args.gradient_accumulation_steps == 0) or (
                idx + 1 == num_update_steps_per_epoch
            ):
                optimizer.step()

            completed_steps += 1
            if (idx + 1) % args.logging_steps == 0:
                report_metrics(
                    local_rank,
                    train_loop_start_time,
                    loss,
                    epoch,
                    completed_steps,
                    samples_processed_per_logging_update,
                    tokens_processed_per_logging_update,
                    "Training",
                )

        dist.barrier()

        if local_rank==0:
            print("######################### Eval #########################")
        eval_start_time = timer()
        
        model.eval()
        eval_running_loss = 0
        for batch in eval_loader:
            with torch.no_grad():
                batch = {k: v.to(device) for k, v, in batch.items()}
                outputs = model(**batch)
            eval_loss = outputs.loss
            eval_running_loss += eval_loss.detach().float() / num_eval_steps_per_epoch

        report_metrics(
            local_rank,
            eval_start_time,
            eval_running_loss,
            epoch,
            completed_steps,
            samples_processed_per_eval,
            tokens_processed_per_eval,
            "Eval",
        )

    # Save checkpoint for evaluation (xm.save ensures only one process save)
    if global_rank == 0:
        model = model.module if hasattr(model, "module") else model
        os.makedirs(args.model_dir, exist_ok=True)
        checkpoint = {"state_dict": model.state_dict()}
        path = f"{args.model_dir}/checkpoint.pt"
        torch.save(checkpoint, path)

        print("##### Model saved to: ", f"{args.model_dir}/checkpoint.pt")
        print(f"Run completed in {timer() - run_start} sec.")


def tokenize_data(examples, tokenizer, sequence_length):
    encoding = tokenizer(
        examples["text"],
        padding="max_length",
        truncation=True,
        max_length=sequence_length,
    )
    return encoding


def get_data(train_sample_count):
    src = "bloyal/oas_paired_human_sars_cov_2"
    test_sample_count = int(train_sample_count * 0.2)
    train_dataset = load_dataset(src, split=f"train[:{train_sample_count}]")
    test_dataset = load_dataset(src, split=f"test[:{test_sample_count}]")
    dataset = DatasetDict({"train": train_dataset, "test": test_dataset}).rename_column(
        "sequence_alignment_aa_heavy", "text"
    )
    tokenizer = AutoTokenizer.from_pretrained("facebook/esm2_t6_8M_UR50D")
    sequence_length = 142
    encoded_dataset = dataset.map(
        tokenize_data,
        batched=True,
        num_proc=os.cpu_count(),
        remove_columns=dataset["train"].column_names,
        fn_kwargs={
            "tokenizer": tokenizer,
            "sequence_length": sequence_length,
        },
    )

    encoded_dataset.set_format("torch", columns=["input_ids", "attention_mask"])
    return encoded_dataset


if __name__ == "__main__":
    args = parse_args()

    ############# Step 2: Compute world size (WORLD_SIZE) using num_gpus and num_nodes
    # and specify the IP address/port number for the node associated with the main process (global rank = 0):
    master = json.loads(os.environ['SM_TRAINING_ENV'])['master_hostname']

    print("[DDP] master address is [{}]".format(master))
    os.environ['MASTER_ADDR'] = master
    os.environ['MASTER_PORT'] = '7777' 
    #####################################################
    torch.multiprocessing.spawn(worker, nprocs=args.num_gpus, args=(args,))