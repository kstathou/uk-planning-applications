import json
import os
from datetime import datetime, timezone

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
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, f"{council}.jsonl")
        with open(filepath, "a") as f:
            f.write(json.dumps(record) + "\n")
        return f"Saved application {reference}"

    return tools
