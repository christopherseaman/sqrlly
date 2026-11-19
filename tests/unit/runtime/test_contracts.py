"""Unit tests for output contract validation."""

from sqrlly.runtime.gates import validate_output_contract
from sqrlly.schema.models import Outputs


class TestValidateOutputs:
    def test_all_files_present(self, tmp_path):
        base = tmp_path / "output"
        base.mkdir()
        (base / "report.md").write_text("content")
        (base / "data.json").write_text("{}")

        contract = Outputs(
            files=['output/report.md', 'output/data.json'],
        )
        assert validate_output_contract(contract, str(tmp_path)) == []

    def test_missing_file(self, tmp_path):
        base = tmp_path / "output"
        base.mkdir()
        (base / "report.md").write_text("content")

        contract = Outputs(
            files=['output/report.md', 'output/missing.txt'],
        )
        missing = validate_output_contract(contract, str(tmp_path))
        assert missing == ["output/missing.txt"]

    def test_multiple_missing(self, tmp_path):
        base = tmp_path / "output"
        base.mkdir()

        contract = Outputs(
            files=['output/a.txt', 'output/b.txt', 'output/c.txt'],
        )
        missing = validate_output_contract(contract, str(tmp_path))
        assert missing == ["output/a.txt", "output/b.txt", "output/c.txt"]

    def test_empty_required_files(self, tmp_path):
        contract = Outputs(
            files=[],
        )
        assert validate_output_contract(contract, str(tmp_path)) == []

    def test_base_directory_missing(self, tmp_path):
        contract = Outputs(
            files=['nonexistent/file.txt'],
        )
        missing = validate_output_contract(contract, str(tmp_path))
        assert missing == ["nonexistent/file.txt"]


def test_validate_output_contract_glob(tmp_path):
    contract = Outputs(files=['out/*.json'])
    (tmp_path / "out").mkdir()
    assert validate_output_contract(contract, str(tmp_path)) == ["out/*.json"]

    (tmp_path / "out" / "a.json").write_text("{}")
    assert validate_output_contract(contract, str(tmp_path)) == []

    literal = Outputs(files=['out/a.json'])
    assert validate_output_contract(literal, str(tmp_path)) == []

    (tmp_path / "out" / "x[1].txt").write_text("x")
    bracket = Outputs(files=['out/x[1].txt'])
    assert validate_output_contract(bracket, str(tmp_path)) == []

    (tmp_path / "out" / "sub").mkdir()
    (tmp_path / "out" / "sub" / "b.md").write_text("x")
    deep = Outputs(
        files=['out/**/b.md', 'out/*.md'],
    )
    assert validate_output_contract(deep, str(tmp_path)) == ["out/*.md"]

    for internal in (".sqrlly/wt-x/out", ".git"):
        (tmp_path / internal).mkdir(parents=True)
        (tmp_path / internal / "report.json").write_text("{}")
    leading = Outputs(files=['**/report.json'])
    assert validate_output_contract(leading, str(tmp_path)) == ["**/report.json"]


def test_globs_stay_inside_tree_and_handle_link_cycles(tmp_path):
    root = tmp_path / "tree"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("secret")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "secret.md").symlink_to(outside / "secret.md")
    (root / "cycle").symlink_to(root, target_is_directory=True)
    files = ["secret.md", "linked/*.md", "**/*.md"]
    assert validate_output_contract(Outputs(files=files), str(root)) == files
    (root / "ok.md").write_text("ok")
    assert validate_output_contract(Outputs(files=files), str(root)) == files[:2]
