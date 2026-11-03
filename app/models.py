"""Fixed local ONNX models reused from the validated pilot, not an answer generator."""
from functools import lru_cache
import hashlib
import json
import logging
from pathlib import Path
import threading
import numpy as np

log = logging.getLogger("techdocs.models")


class Models:
    def __init__(self, directory: Path, enabled=True):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.dense = None
        self.encoder = None
        self.decoder = None
        self.model_tag = None
        self.errors = []
        if not enabled:
            return
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        opts.inter_op_num_threads = 1
        e5 = self.directory / "e5"
        mt = self.directory / "translation"
        try:
            from tokenizers import Tokenizer
            self._verify_assets("e5")
            self.tokenizer = Tokenizer.from_file(str(e5 / "tokenizer.json"))
            self.tokenizer.enable_truncation(max_length=384)
            self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
            self.dense = ort.InferenceSession(str(e5 / "model_quantized.onnx"), sess_options=opts, providers=["CPUExecutionProvider"])
            self.model_tag = hashlib.sha256((e5 / "model_quantized.onnx").read_bytes()).hexdigest()
        except Exception as exc:
            self.errors.append("向量模型未就绪：" + type(exc).__name__)
            log.warning("Dense model unavailable: %s", type(exc).__name__)
        try:
            import sentencepiece as spm
            self._verify_assets("translation")
            self.spm_source = spm.SentencePieceProcessor(model_file=str(mt / "source.spm"))
            self.spm_target = spm.SentencePieceProcessor(model_file=str(mt / "target.spm"))
            self.vocab = json.loads((mt / "vocab.json").read_text(encoding="utf-8"))
            self.reverse = {v: k for k, v in self.vocab.items()}
            self.encoder = ort.InferenceSession(str(mt / "encoder_model_quantized.onnx"), sess_options=opts, providers=["CPUExecutionProvider"])
            self.decoder = ort.InferenceSession(str(mt / "decoder_model_quantized.onnx"), sess_options=opts, providers=["CPUExecutionProvider"])
        except Exception as exc:
            self.encoder = self.decoder = None
            self.errors.append("中文翻译模型未就绪：" + type(exc).__name__)
            log.warning("Translation model unavailable: %s", type(exc).__name__)

    def _verify_assets(self, family):
        manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
        assets = [a for a in manifest["assets"] if a["family"] == family]
        if not assets:
            raise ValueError("Model family absent from manifest")
        for asset in assets:
            path = self.directory / family / asset["filename"]
            if path.stat().st_size != asset["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
                raise ValueError("Model checksum mismatch")

    @property
    def dense_ready(self):
        return self.dense is not None

    @property
    def translation_ready(self):
        return self.encoder is not None and self.decoder is not None

    def embed(self, texts: list[str], prefix: str):
        if not self.dense_ready:
            return None
        result = []
        with self.lock:
            for offset in range(0, len(texts), 8):
                enc = self.tokenizer.encode_batch([prefix + t for t in texts[offset:offset + 8]])
                ids = np.array([t.ids for t in enc], dtype=np.int64)
                mask = np.array([t.attention_mask for t in enc], dtype=np.int64)
                values = {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)}
                hidden = self.dense.run(None, {i.name: values[i.name] for i in self.dense.get_inputs()})[0]
                vec = (hidden * mask[:, :, None]).sum(axis=1) / mask.sum(axis=1)[:, None]
                vec = vec / (np.linalg.norm(vec, axis=1, keepdims=True) + 1e-12)
                result.append(vec.astype(np.float32))
        return np.concatenate(result) if result else np.empty((0, 384), dtype=np.float32)

    @lru_cache(maxsize=256)
    def translate(self, text: str):
        if not self.translation_ready:
            raise RuntimeError("中文翻译模型未加载；请安装固定模型，或直接搜索英文关键词")
        with self.lock:
            pieces = self.spm_source.encode(text, out_type=str)
            if len(pieces) > 383:
                raise ValueError("中文问题过长，请缩短后搜索")
            ids = np.array([[self.vocab.get(p, 1) for p in pieces] + [0]], dtype=np.int64)
            mask = np.ones_like(ids)
            hidden = self.encoder.run(None, {"input_ids": ids, "attention_mask": mask})[0]
            output, ended = [65000], False
            for _ in range(95):
                values = {"input_ids": np.array([output], dtype=np.int64), "encoder_hidden_states": hidden, "encoder_attention_mask": mask}
                logits = self.decoder.run(["logits"], {i.name: values[i.name] for i in self.decoder.get_inputs()})[0][0, -1].copy()
                logits[65000] = -np.inf
                nxt = int(np.argmax(logits))
                output.append(nxt)
                if nxt == 0:
                    ended = True
                    break
            translated = self.spm_target.decode_pieces([self.reverse[i] for i in output if i not in [0, 65000]])
            return {"text": translated, "complete": ended}
