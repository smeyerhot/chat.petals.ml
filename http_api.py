import threading
from contextlib import nullcontext
from traceback import format_exc
from uuid import uuid4
import os

import torch
import transformers
import wandb
from datasets import load_dataset
from tqdm import tqdm
from torch.optim import AdamW
from torch.utils.data import DataLoader
from transformers import BloomTokenizerFast, get_scheduler

from petals import DistributedBloomForCausalLM
import hivemind
from flask import jsonify, request

import config
from app import app, models

logger = hivemind.get_logger(__file__)


storage_lock = threading.Lock()
inference_sessions = hivemind.TimedStorage()  # Should be used under storage_lock

@app.get("/api/v1/train")
def train():
    # os.environ['HIVEMIND_LOGLEVEL'] = "DEBUG"
    # os.environ['GOLOG_LOG_LEVEL'] = "DEBUG"

    # MODEL_NAME = "./model_save"
    MODEL_NAME = "bigscience/bloomz-petals"
    TOK_NAME = "bigscience/bloomz-petals"
    TUNING_MODE = 'ptune'
    NUM_PREFIX_TOKENS = 4
    DEVICE = "cuda"
    BATCH_SIZE = 1
    LR = 1e-2
    WEIGHT_DECAY = 0.0
    NUM_SAMPLES = 10
    SEED = 42
    MODEL_MAX_LENGTH = 256
    tokenizer = BloomTokenizerFast.from_pretrained(TOK_NAME)
    tokenizer.padding_side = 'right'
    tokenizer.model_max_length = MODEL_MAX_LENGTH
    model = DistributedBloomForCausalLM.from_pretrained(
        MODEL_NAME,
        pre_seq_len=NUM_PREFIX_TOKENS, 
        tuning_mode=TUNING_MODE,
        request_timeout=300
    ).to(DEVICE)

    dataset = load_dataset("bavard/personachat_truecased")

    def chunking(examples):
        inputs = [
            "\n-----\n".join(history) + "\n-----\n" + candidate
            for history, candidates in zip(examples["history"], examples["candidates"])
            for candidate in candidates
        ]
        return {"chunks": inputs}


    def tokenize(examples):
        outputs = {
            "input_ids": tokenizer(examples["chunks"], padding='max_length', truncation=True)["input_ids"]
        }
        outputs["labels"] = outputs["input_ids"]
        return outputs


    tokenized_datasets = (
        dataset
            .map(chunking, batched=True, remove_columns=dataset["train"].column_names)
            .map(tokenize, batched=True, remove_columns=["chunks"])
    )

    tokenized_datasets.set_format("torch")
    train_dataset = tokenized_datasets["train"].shuffle(seed=SEED)
    train_dataloader = DataLoader(
        train_dataset.select(list(range(NUM_SAMPLES))),
        shuffle=True,
        batch_size=BATCH_SIZE,
        drop_last=True,
    )

    for n, p in model.named_parameters():
        if p.requires_grad:
            print(n, p.requires_grad, p.device)

    optimizer = AdamW(model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)

    lr_scheduler = get_scheduler(
        name="linear", optimizer=optimizer, num_warmup_steps=0, num_training_steps=len(train_dataloader)
    )

    wandb.init(
        project="bloom-personachat",
        config={
            "num_samples": NUM_SAMPLES,
            "batch_size": BATCH_SIZE,
            "learning_rate": LR,
            "weight_decay": WEIGHT_DECAY,
            "num_prefix_tokens": NUM_PREFIX_TOKENS,
            "model_name": MODEL_NAME,
            "seed": SEED,
        }
    )

    for batch in tqdm(train_dataloader):
        batch = {k: v.to(DEVICE) for k, v in batch.items()}

        model.train()
        outputs = model(**batch)
        loss = outputs.loss
        loss.backward()

        optimizer.step()
        lr_scheduler.step()
        optimizer.zero_grad()

        wandb.log({"Train Loss": loss})
    
    output_dir = './model_save/checkpoint_psize_64'

    # Create output directory if needed
    # Save a trained model, configuration and tokenizer using `save_pretrained()`.
    # They can then be reloaded using `from_pretrained()`
    model_to_save = model.module if hasattr(model, 'module') else model  # Take care of distributed/parallel training
    model_to_save.save_pretrained(output_dir)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    
@app.get("/api/v1/open_inference_session")
def http_api_open_inference_session():
    try:
        model_name = get_typed_arg("model", str, config.DEFAULT_MODEL_NAME)
        max_length = get_typed_arg("max_length", int, 1024)
        logger.info(f"open_inference_session(), model={repr(model_name)}, max_length={max_length}")

        model, _ = models[model_name]
        with storage_lock:
            if len(inference_sessions) >= config.MAX_SESSIONS:
                raise RuntimeError(
                    f"Too many opened inference sessions (max {config.MAX_SESSIONS}), please come back later"
                )
            # We don't release the lock here so that a concurrent thread else does not occupy our place.
            # session.__init__() and __enter__() are fast enough for that.

            session = model.inference_session(max_length=max_length)
            session.__enter__()
            session_lock = threading.Lock()

            session_id = uuid4().hex
            inference_sessions.store(
                session_id,
                (session, session_lock),
                hivemind.get_dht_time() + config.STEP_TIMEOUT,
            )

        return jsonify(ok=True, session_id=session_id)
    except Exception:
        return jsonify(ok=False, traceback=format_exc())


@app.get("/api/v1/close_inference_session")
def http_api_close_inference_session():
    try:
        session_id = request.values.get("session_id")
        logger.info(f"close_inference_session(), session_id={repr(session_id)}")

        with storage_lock:
            del inference_sessions[session_id]

        return jsonify(ok=True, session_id=session_id)
    except Exception:
        return jsonify(ok=False, traceback=format_exc())


@app.post("/api/v1/generate")
def http_api_generate():
    try:
        model_name = get_typed_arg("model", str, config.DEFAULT_MODEL_NAME)
        inputs = request.values.get("inputs")
        do_sample = get_typed_arg("do_sample", int, 0)
        temperature = get_typed_arg("temperature", float, 1.0)
        top_k = get_typed_arg("top_k", int)
        top_p = get_typed_arg("top_p", float)
        max_length = get_typed_arg("max_length", int)
        max_new_tokens = get_typed_arg("max_new_tokens", int)
        session_id = request.values.get("session_id")
        logger.info(f"generate(), model={repr(model_name)}, session_id={repr(session_id)}, inputs={repr(inputs)}")

        model, tokenizer = models[model_name]
        if inputs is not None:
            inputs = tokenizer(inputs, return_tensors="pt")["input_ids"].to(config.DEVICE)
            n_input_tokens = inputs.shape[1]
        else:
            n_input_tokens = 0

        if session_id is not None:
            with storage_lock:
                if session_id not in inference_sessions:
                    raise KeyError(f"Session {repr(session_id)} expired or does not exist")
                session, session_lock = inference_sessions.get(session_id).value
                inference_sessions.store(
                    session_id,
                    (session, session_lock),
                    hivemind.get_dht_time() + config.STEP_TIMEOUT,
                )
        else:
            session = None
            session_lock = nullcontext()

        with session_lock:
            outputs = model.generate(
                inputs=inputs,
                do_sample=do_sample,
                temperature=temperature,
                top_k=top_k,
                top_p=top_p,
                max_length=max_length,
                max_new_tokens=max_new_tokens,
                session=session,
            )
        outputs = tokenizer.decode(outputs[0, n_input_tokens:])
        logger.info(f"generate(), outputs={repr(outputs)}")

        return jsonify(ok=True, outputs=outputs)
    except Exception:
        return jsonify(ok=False, traceback=format_exc())


def get_typed_arg(name, expected_type, default=None):
    value = request.values.get(name)
    return expected_type(value) if value is not None else default
