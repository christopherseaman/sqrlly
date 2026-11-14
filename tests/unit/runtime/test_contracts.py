"""Unit tests for output contract validation."""

from sqrlly.runtime.gates import validate_output_contract
from sqrlly.schema.models import OutputContract


class TestValidateOutputContract:
    def test_all_files_present(self, tmp_path):
        base = tmp_path / "output"
        base.mkdir()
        (base / "report.md").write_text("content")
        (base / "data.json").write_text("{}")

        contract = OutputContract(
            base_directory="output",
            required_files=["report.md", "data.json"],
        )
        assert validate_output_contract(contract, str(tmp_path)) == []

    def test_missing_file(self, tmp_path):
        base = tmp_path / "output"
        base.mkdir()
        (base / "report.md").write_text("content")

        contract = OutputContract(
            base_directory="output",
            required_files=["report.md", "missing.txt"],
        )
        missing = validate_output_contract(contract, str(tmp_path))
        assert missing == ["output/missing.txt"]

    def test_multiple_missing(self, tmp_path):
        base = tmp_path / "output"
        base.mkdir()

        contract = OutputContract(
            base_directory="output",
            required_files=["a.txt", "b.txt", "c.txt"],
        )
        missing = validate_output_contract(contract, str(tmp_path))
        assert missing == ["output/a.txt", "output/b.txt", "output/c.txt"]

    def test_empty_required_files(self, tmp_path):
        contract = OutputContract(
            base_directory="output",
            required_files=[],
        )
        assert validate_output_contract(contract, str(tmp_path)) == []

    def test_base_directory_missing(self, tmp_path):
        contract = OutputContract(
            base_directory="nonexistent",
            required_files=["file.txt"],
        )
        missing = validate_output_contract(contract, str(tmp_path))
        assert missing == ["nonexistent/file.txt"]


class TestRequiredPaths:
    def test_prepends_base_directory(self):
        contract = OutputContract(
            base_directory="reference",
            required_files=["prd-context-map.json", "sub/x.txt"],
        )
        assert contract.required_paths() == [
            "reference/prd-context-map.json",
            "reference/sub/x.txt",
        ]

    def test_dot_base_collapses_to_bare_name(self):
        contract = OutputContract(base_directory=".", required_files=["x.json"])
        assert contract.required_paths() == ["x.json"]

    def test_empty_required_files(self):
        contract = OutputContract(base_directory="reference", required_files=[])
        assert contract.required_paths() == []


def test_validate_output_contract_glob(tmp_path):
    contract = OutputContract(base_directory=".", required_files=["out/*.json"])
    (tmp_path / "out").mkdir()
    assert validate_output_contract(contract, str(tmp_path)) == ["out/*.json"]

    (tmp_path / "out" / "a.json").write_text("{}")
    assert validate_output_contract(contract, str(tmp_path)) == []

    literal = OutputContract(base_directory="out", required_files=["a.json"])
    assert validate_output_contract(literal, str(tmp_path)) == []

    (tmp_path / "out" / "x[1].txt").write_text("x")
    bracket = OutputContract(base_directory="out", required_files=["x[1].txt"])
    assert validate_output_contract(bracket, str(tmp_path)) == []

    (tmp_path / "out" / "sub").mkdir()
    (tmp_path / "out" / "sub" / "b.md").write_text("x")
    deep = OutputContract(
        base_directory="out", required_files=["**/b.md", "*.md"],
    )
    assert validate_output_contract(deep, str(tmp_path)) == ["out/*.md"]

    for internal in (".sqrlly/wt-x/out", ".git"):
        (tmp_path / internal).mkdir(parents=True)
        (tmp_path / internal / "report.json").write_text("{}")
    leading = OutputContract(base_directory=".", required_files=["**/report.json"])
    assert validate_output_contract(leading, str(tmp_path)) == ["**/report.json"]
