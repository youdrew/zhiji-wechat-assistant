"""Core-owned, opt-in semantic retrieval through the shared model asset downloader.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations
import math
from core_model_assets import ModelAssetError, ModelAssets, _Redirects as AssetRedirects

class EmbeddingError(ModelAssetError):
    pass

class _Redirects(AssetRedirects):
    def __init__(self, hosts):
        super().__init__(hosts, error_type=EmbeddingError, code_prefix="embedding")

class EmbeddingService(ModelAssets):
    def __init__(self, root, directory, gateway, *, manifest_path=None, opener=None):
        super().__init__(root, directory, gateway, manifest_path=manifest_path, opener=opener,
            manifest_name="embedding-model.json", label="检索模型", code_prefix="embedding", error_type=EmbeddingError)

    def _manifest(self):
        value = super()._manifest()
        if type(value.get("dimensions")) is not int or not 1 <= value["dimensions"] <= 4096:
            raise EmbeddingError("embedding-manifest", "内置检索模型清单不可用，请更新软件")
        return value

    def status(self):
        return super().status() | {"dimensions": self.manifest["dimensions"]}

    def embed(self, texts, kind="passage", *, plugin="kyf.self-analysis", check=None):
        maximum = 4000 if kind == "query" else 2400
        if (kind not in ("passage", "query") or not isinstance(texts, list) or not 1 <= len(texts) <= 16 or
                any(not isinstance(text, str) or not text.strip() or len(text) > maximum or "\x00" in text for text in texts)):
            raise EmbeddingError("embedding-invalid", "每批最多 16 条文本；查询不超过 4000 字，样本不超过 2400 字")
        if check:
            check()
        status = self.status()
        if not status["ready"]:
            raise EmbeddingError("embedding-not-ready", "请先准备本机语义检索模型")
        try:
            result = self.gateway.embedding({"modelDir": str(self.directory.resolve()),
                "modelId": status["modelId"], "revision": status["revision"], "dimensions": status["dimensions"],
                "texts": texts, "kind": kind, "plugin": plugin}, timeout=2 if kind == "query" else 30, check=check)
        finally:
            if check:
                check()
        vectors = result.get("vectors") if isinstance(result, dict) else None
        if (not isinstance(result, dict) or any(result.get(key) != status[key]
                for key in ("modelId", "revision", "dimensions")) or
                type(result.get("dimensions")) is not int or not isinstance(vectors, list) or
                len(vectors) != len(texts) or any(not self._valid_vector(vector, status["dimensions"]) for vector in vectors)):
            raise EmbeddingError("embedding-output", "语义检索模型返回无效向量")
        if self.closed:
            raise EmbeddingError("embedding-closed", "应用正在关闭")
        return {key: status[key] for key in ("modelId", "revision", "dimensions")} | {"vectors": vectors}

    @staticmethod
    def _valid_vector(vector, dimensions):
        try:
            return (isinstance(vector, list) and len(vector) == dimensions and
                all(type(number) in (int, float) and math.isfinite(number) for number in vector) and
                1e-12 < sum(number * number for number in vector) < float("inf"))
        except (ValueError, OverflowError):
            return False
