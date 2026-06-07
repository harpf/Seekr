from document_search.config import AppConfig, load_config


def test_appconfig_has_semantic_defaults():
    cfg = AppConfig()
    assert cfg.semantic_search_enabled is False
    assert cfg.embed_model == "nomic-embed-text"
    assert cfg.bm25_weight == 1.0
    assert cfg.vector_weight == 1.0


def test_load_config_reads_semantic_flags(tmp_path):
    import json
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "semantic_search_enabled": True,
        "embed_model": "mxbai-embed-large",
        "bm25_weight": 2.0,
        "vector_weight": 0.5,
    }), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.semantic_search_enabled is True
    assert cfg.embed_model == "mxbai-embed-large"
    assert cfg.bm25_weight == 2.0
    assert cfg.vector_weight == 0.5
