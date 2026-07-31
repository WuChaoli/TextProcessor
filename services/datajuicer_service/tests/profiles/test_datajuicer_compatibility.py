from pathlib import Path

from datajuicer_service.profiles.compatibility import verify_datajuicer_runtime


def test_runtime_is_pinned_datajuicer_source() -> None:
    runtime = verify_datajuicer_runtime()

    assert runtime.version == "1.5.4"
    assert runtime.commit == "7061da6ad06287aa0305eda162429b34361a56a3"
    assert Path(runtime.import_path).as_posix().endswith(
        "vendor/data-juicer/data_juicer/__init__.py"
    )
    assert runtime.num_bands == 25
    assert runtime.num_rows_per_band == 10
    assert runtime.signature_bands == 25
    assert runtime.signature_band_bytes == 80
    assert (
        runtime.signature_sha256
        == "36c4a3957476101bd9d5e9cc03178adc40e2f4c36b8de455112b57145028d294"
    )
