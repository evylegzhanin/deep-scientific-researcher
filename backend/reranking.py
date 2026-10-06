"""Local rerankers selected by checkpoint architecture, without random heads."""
from __future__ import annotations

from threading import Lock


class QwenYesNoReranker:
    """Use the pretrained causal LM head following Qwen3 Reranker's model card."""
    outputs_probability = True

    def __init__(self, path: str, *, batch_size: int = 4, max_length: int = 2048):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(path, padding_side="left", local_files_only=True)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.model = AutoModelForCausalLM.from_pretrained(
            path, torch_dtype=torch.float32, local_files_only=True,
        ).eval()
        self.model.config.pad_token_id = self.tokenizer.pad_token_id
        self.yes = self.tokenizer.convert_tokens_to_ids("yes")
        self.no = self.tokenizer.convert_tokens_to_ids("no")
        self.prefix = self.tokenizer.encode(
            '<|im_start|>system\nJudge whether the Document meets the requirements based on '
            'the Query and the Instruct provided. Note that the answer can only be "yes" '
            'or "no".<|im_end|>\n<|im_start|>user\n', add_special_tokens=False,
        )
        self.suffix = self.tokenizer.encode(
            '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n',
            add_special_tokens=False,
        )
        self.batch_size = batch_size
        self.max_length = max_length
        self.lock = Lock()

    @staticmethod
    def format_pair(query: str, document: str) -> str:
        return ("<Instruct>: Given a web search query, retrieve relevant passages that answer the query"
                f"\n<Query>: {query}\n<Document>: {document}")

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        import torch

        scores = []
        # API uploads and concurrent research may share this model instance.
        with self.lock, torch.inference_mode():
            for start in range(0, len(pairs), self.batch_size):
                encoded = self.tokenizer(
                    [self.format_pair(*pair) for pair in pairs[start:start + self.batch_size]],
                    padding=False, truncation=True, add_special_tokens=False,
                    max_length=self.max_length - len(self.prefix) - len(self.suffix),
                    return_attention_mask=False,
                )
                encoded["input_ids"] = [self.prefix + ids + self.suffix for ids in encoded["input_ids"]]
                inputs = self.tokenizer.pad(encoded, padding=True, return_tensors="pt")
                # Only the final-token logits are needed; avoid a full sequence/vocabulary tensor.
                logits = self.model(**inputs, logits_to_keep=1).logits[:, -1, :]
                probabilities = torch.softmax(logits[:, [self.no, self.yes]].float(), dim=-1)
                scores.extend(probabilities[:, 1].tolist())
        return scores


def load_reranker(path: str):
    from transformers import AutoConfig
    from sentence_transformers import CrossEncoder

    config = AutoConfig.from_pretrained(path, local_files_only=True)
    architectures = config.architectures or []
    if "Qwen3ForCausalLM" in architectures:
        return QwenYesNoReranker(path)
    if not any(name.endswith("ForSequenceClassification") for name in architectures):
        raise ValueError(f"Unsupported reranker architecture: {architectures}")
    return CrossEncoder(path)
