import asyncio
import json
import os
import tempfile

from backend.tools.save import create_save_tools


def test_save_application_writes_jsonl():
    with tempfile.TemporaryDirectory() as tmpdir:
        tools = create_save_tools(output_dir=tmpdir)

        # Find the save_application action function
        save_fn = None
        for action in tools.registry.registry.actions.values():
            if action.name == "save_application":
                save_fn = action.function
                break
        assert save_fn is not None, "save_application tool not registered"

        result = asyncio.run(
            save_fn(
                reference="HGY/2026/0001",
                address="123 High Road, London N22",
                description="Single storey rear extension",
                status="pending",
                submitted_date="2026-03-01",
                decision_date="",
                applicant="John Smith",
                council="haringey",
                url="https://planningservices.haringey.gov.uk/portal/123",
                raw_fields={"ward": "Hornsey", "officer": "Jane Doe"},
            )
        )

        assert "Saved" in result

        filepath = os.path.join(tmpdir, "haringey.jsonl")
        assert os.path.exists(filepath)

        with open(filepath) as f:
            line = f.readline()
            data = json.loads(line)

        assert data["reference"] == "HGY/2026/0001"
        assert data["council"] == "haringey"
        assert data["raw_fields"]["ward"] == "Hornsey"
        assert data["url"] == "https://planningservices.haringey.gov.uk/portal/123"


def test_save_application_appends_multiple():
    with tempfile.TemporaryDirectory() as tmpdir:
        tools = create_save_tools(output_dir=tmpdir)
        save_fn = None
        for action in tools.registry.registry.actions.values():
            if action.name == "save_application":
                save_fn = action.function
                break

        for i in range(3):
            asyncio.run(
                save_fn(
                    reference=f"REF/{i}",
                    address=f"Address {i}",
                    description="Desc",
                    status="pending",
                    submitted_date="2026-01-01",
                    decision_date="",
                    applicant="Applicant",
                    council="test_council",
                    url=f"https://example.com/{i}",
                    raw_fields={},
                )
            )

        filepath = os.path.join(tmpdir, "test_council.jsonl")
        with open(filepath) as f:
            lines = f.readlines()
        assert len(lines) == 3
