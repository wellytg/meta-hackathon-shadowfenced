"""QLoRA SFT of Llama 3.2 3B on the chat format "message -> verdict JSON".

Fine-tunes ``meta-llama/Llama-3.2-3B-Instruct`` with 4-bit quantization
(bitsandbytes NF4), LoRA ``r=16``, on the train split from ``ft_split.py``
(chat prompt: the workplace message; completion: the verdict JSON).
Sized for a single 12 GB GPU: 4-bit base weights (~2 GB), ``max_len=1024``,
per-device batch 1 with 16 gradient-accumulation steps (effective 16),
gradient checkpointing, paged 8-bit AdamW. Saves only the LoRA adapter to
``runs/ft/adapter/``.

Usage::

    python scripts/ft_train.py [--train runs/ft/train.jsonl]
                               [--val runs/ft/val.jsonl]
                               [--out runs/ft/adapter]
                               [--epochs 3] [--seed 7]

Heavy imports (torch/transformers/peft/bitsandbytes) live inside
``train()`` so importing this module (and its pure helpers) needs no GPU
and no extra dependencies. Needs network (Hub weights) + GPU to run.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MODEL_ID = "meta-llama/Llama-3.2-3B-Instruct"

SYSTEM_PROMPT = (
    "Classify text a user is about to send to an outside AI. "
    "beacon = reveals where secrets or personal data are stored or were moved. "
    "span = the shortest exact sensitive phrase copied from the text, "
    "at most 8 words, or empty. JSON only."
)

VERDICT_KEYS = ("cls", "is_beacon", "sensitivity", "confidence", "span")


@dataclass
class TrainConfig:
    """Memory-sized defaults for one 12 GB GPU."""

    model_id: str = MODEL_ID
    max_len: int = 1024
    per_device_batch: int = 1
    grad_accum: int = 16
    epochs: float = 3.0
    lr: float = 2e-4
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    seed: int = 7


def verdict_json(verdict: dict) -> str:
    """Canonical completion string: verdict JSON with fixed keys."""
    return json.dumps({key: verdict[key] for key in VERDICT_KEYS},
                      ensure_ascii=False)


def build_chat_text(text: str, verdict: dict | None = None) -> str:
    """Chat-format training string: system + user, plus assistant when the
    verdict is given (train rows), without it for inference prompts."""
    head = (f"<|system|>\n{SYSTEM_PROMPT}\n"
            f"<|user|>\n{text}\n<|assistant|>\n")
    if verdict is None:
        return head
    return head + verdict_json(verdict)


def lora_config_dict(cfg: TrainConfig) -> dict:
    """peft LoraConfig kwargs (r=16, targets cover attn + MLP)."""
    return {
        "r": cfg.lora_r,
        "lora_alpha": cfg.lora_alpha,
        "lora_dropout": cfg.lora_dropout,
        "bias": "none",
        "task_type": "CAUSAL_LM",
        "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj",
                           "gate_proj", "up_proj", "down_proj"],
    }


def quant_config_dict() -> dict:
    """bitsandbytes 4-bit NF4 kwargs for a 12 GB card."""
    return {
        "load_in_4bit": True,
        "bnb_4bit_quant_type": "nf4",
        "bnb_4bit_compute_dtype": "bfloat16",
        "bnb_4bit_use_double_quant": True,
    }


def load_examples(train_path: Path, val_path: Path,
                  max_len: int) -> tuple[list[str], list[str]]:
    """Read train/val JSONL; return formatted chat strings.

    Only the train/val files are read here: the test split is never
    loaded for training.
    """
    def read(path: Path) -> list[str]:
        rows = [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]
        texts = []
        for row in rows:
            full = build_chat_text(row["text"], row["verdict"])
            texts.append(full[:max_len * 4])  # ~4 chars/token guard pre-tokenize
        return texts

    return read(train_path), read(val_path)


def train(cfg: TrainConfig, train_path: Path, val_path: Path,
          out_dir: Path) -> Path:
    """Run QLoRA SFT; save the adapter to ``out_dir``. Needs GPU + Hub."""
    import torch  # noqa: E402
    from datasets import Dataset  # noqa: E402
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training  # noqa: E402
    from transformers import (AutoModelForCausalLM, AutoTokenizer,  # noqa: E402
                              BitsAndBytesConfig, TrainingArguments)
    try:
        from trl import SFTTrainer  # noqa: E402
        has_sft = True
    except ImportError:
        has_sft = False

    train_texts, val_texts = load_examples(train_path, val_path, cfg.max_len)
    tokenizer = AutoTokenizer.from_pretrained(cfg.model_id)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        cfg.model_id,
        quantization_config=BitsAndBytesConfig(**quant_config_dict()),
        device_map="auto",
    )
    model = prepare_model_for_kbit_training(model)
    model = get_peft_model(model, LoraConfig(**lora_config_dict(cfg)))

    args = TrainingArguments(
        output_dir=str(out_dir / "checkpoints"),
        per_device_train_batch_size=cfg.per_device_batch,
        gradient_accumulation_steps=cfg.grad_accum,
        num_train_epochs=cfg.epochs,
        learning_rate=cfg.lr,
        optim="paged_adamw_8bit",
        gradient_checkpointing=True,
        logging_steps=10,
        save_steps=100,
        seed=cfg.seed,
        bf16=torch.cuda.is_bf16_supported(),
        report_to="none",
    )
    if has_sft:
        trainer = SFTTrainer(
            model=model,
            args=args,
            train_dataset=Dataset.from_dict({"text": train_texts}),
            eval_dataset=Dataset.from_dict({"text": val_texts}),
            processing_class=tokenizer,
            max_seq_length=cfg.max_len,
        )
    else:
        from transformers import Trainer  # noqa: E402

        def tokenize(batch):
            return tokenizer(batch["text"], truncation=True,
                             max_length=cfg.max_len)

        train_ds = Dataset.from_dict({"text": train_texts}).map(
            tokenize, batched=True, remove_columns=["text"])
        val_ds = Dataset.from_dict({"text": val_texts}).map(
            tokenize, batched=True, remove_columns=["text"])
        trainer = Trainer(model=model, args=args,
                          train_dataset=train_ds, eval_dataset=val_ds)
    trainer.train()
    out_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(out_dir))
    tokenizer.save_pretrained(str(out_dir))
    from dataclasses import asdict as _asdict

    (out_dir / "train_config.json").write_text(
        json.dumps(_asdict(cfg), indent=2), encoding="utf-8")
    print(f"saved adapter to {out_dir}")
    return out_dir


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", default=str(ROOT / "runs" / "ft" / "train.jsonl"))
    parser.add_argument("--val", default=str(ROOT / "runs" / "ft" / "val.jsonl"))
    parser.add_argument("--out", default=str(ROOT / "runs" / "ft" / "adapter"))
    parser.add_argument("--epochs", type=float, default=3.0)
    parser.add_argument("--seed", type=int, default=7)
    return parser.parse_args(argv)


def main(argv=None) -> Path:
    args = _parse_args(argv)
    cfg = TrainConfig(epochs=args.epochs, seed=args.seed)
    return train(cfg, Path(args.train), Path(args.val), Path(args.out))


if __name__ == "__main__":
    main()
