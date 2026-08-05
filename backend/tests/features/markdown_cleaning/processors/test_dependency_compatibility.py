def test_markdown_cleaning_processor_dependencies_are_importable_on_py3_14() -> None:
    import importlib

    import mdformat
    import mdformat_gfm
    from markdown_it import MarkdownIt
    from mdformat import __version__ as _

    importlib.import_module("presidio_analyzer")

    from presidio_analyzer import AnalyzerEngine

    assert MarkdownIt is not None
    assert AnalyzerEngine is not None
    assert _ is not None
    assert mdformat is not None
    assert mdformat_gfm is not None
