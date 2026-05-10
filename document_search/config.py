from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
try:
    import yaml
except ModuleNotFoundError:
    yaml = None


@dataclass(slots=True)
class OcrConfig:
    enabled: bool = False
    languages: list[str] = field(default_factory=lambda: ["deu", "eng"])


@dataclass(slots=True)
class AppConfig:
    database_path: Path = Path("./document_index.db")
    supported_extensions: list[str] = field(default_factory=lambda: [".pdf", ".docx", ".pptx", ".txt", ".md", ".doc", ".ppt"])
    exclude_dirs: list[str] = field(default_factory=lambda: [".git", "node_modules", "__pycache__", ".venv", "temp"])
    exclude_patterns: list[str] = field(default_factory=lambda: ["~$*", "*.tmp"])
    max_file_size_mb: int = 100
    logging_level: str = "INFO"
    default_limit: int = 20
    ignore_hidden: bool = True
    ignore_temp_office_files: bool = True
    follow_symlinks: bool = False
    ocr: OcrConfig = field(default_factory=OcrConfig)


def load_config(path: Path | None) -> AppConfig:
    if path is None or not path.exists():
        return AppConfig()
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        if yaml is None:
            raise RuntimeError("PyYAML is required for YAML config files")
        raw = yaml.safe_load(text)
    else:
        raw = json.loads(text)
    cfg = AppConfig()
    for key, value in raw.items():
        if key == "ocr" and isinstance(value, dict):
            cfg.ocr = OcrConfig(**value)
        elif hasattr(cfg, key):
            setattr(cfg, key, value)
    if isinstance(cfg.database_path, str):
        cfg.database_path = Path(cfg.database_path)
    return cfg
