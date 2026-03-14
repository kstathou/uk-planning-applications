import json
import os
from datetime import UTC, datetime

from browser_use import Tools


def create_save_tools(output_dir: str = "data") -> Tools:
    """Create a Tools instance with the save_application action."""
    tools = Tools()

    @tools.action(
        description=(
            "Save a planning application to the output file. "
            "Call this for EACH application you find on the portal. "
            "Put any fields that don't fit the named parameters into raw_fields."
        )
    )
    def save_application(
        reference: str,
        address: str,
        description: str,
        status: str,
        submitted_date: str,
        decision_date: str,
        applicant: str,
        council: str,
        url: str,
        raw_fields: dict,
    ) -> str:
        record = {
            "reference": reference,
            "address": address,
            "description": description,
            "status": status,
            "submitted_date": submitted_date,
            "decision_date": decision_date,
            "applicant": applicant,
            "council": council,
            "url": url,
            "raw_fields": raw_fields,
            "scraped_at": datetime.now(UTC).isoformat(),
        }
        os.makedirs(output_dir, exist_ok=True)
        safe_council = council.replace(os.sep, "_").replace("/", "_").replace("..", "_")
        filepath = os.path.join(output_dir, f"{safe_council}.jsonl")
        with open(filepath, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return f"Saved application {reference}"

    return tools
