import os
import json
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
import anthropic

load_dotenv()

SHEET_ID = "1tkFlhw-zc3-jU9qfY4TGJ0-9Q2-4fScpKerfU_jYPRI"
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

WRITE_NEW_STATUS_IF_BLANK = True

PROTECTED_STATUSES = {"Contacted", "Diagnostic Scheduled"}
AGENT_ALLOWED_FINAL_STATUSES = {"Assessed", "Declined"}

CRM_STATUS_COLUMN = "CRM status"
AGENT_SCORE_COLUMN = "Agent Score"
RATIONALE_COLUMN = "Rationale"
PROPOSED_TIER_COLUMN = "Proposed Tier"
FOLLOWUP_DRAFT_COLUMN = "Follow-up Draft"

creds = Credentials.from_service_account_file("service_account.json").with_scopes(SCOPES)
gc = gspread.authorize(creds)
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


def load_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def get_system_instructions():
    persona = load_file("prompts/system_persona.md")
    core = load_file("context/pishi_strategic_core.md")
    icp = load_file("context/icp_definition.md")
    service_architecture = load_file("context/service_architecture.md")
    return f"{persona}\n\n{core}\n\n{icp}\n\n{service_architecture}"


def analyze_lead(lead_text):
    system_prompt = get_system_instructions()
    task_prompt = load_file("prompts/lead_qualifier.md")

    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1500,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": f"{task_prompt}\n\nLEAD DATA:\n{lead_text}",
            }
        ],
    )

    text_blocks = [
        block.text for block in response.content
        if getattr(block, "type", None) == "text"
    ]
    return "".join(text_blocks).strip()


def extract_json_from_response(response_text):
    cleaned = response_text.strip()
    start_index = cleaned.find("{")
    if start_index == -1:
        raise ValueError("No JSON object found in response.")

    decoder = json.JSONDecoder()
    parsed, _ = decoder.raw_decode(cleaned[start_index:])
    return parsed


def normalize_crm_status(value):
    return (value or "").strip()


def get_row_crm_status(row):
    return normalize_crm_status(str(row.get(CRM_STATUS_COLUMN, "")))


def should_process_row(row):
    crm_status = get_row_crm_status(row)
    agent_score = str(row.get(AGENT_SCORE_COLUMN, "")).strip()
    followup = str(row.get(FOLLOWUP_DRAFT_COLUMN, "")).strip()

    if crm_status in PROTECTED_STATUSES:
        return False

    if agent_score or followup:
        return False

    if crm_status in {"", "New"}:
        return True

    return False


def build_lead_text(row):
    return (
        f"Full Name: {row.get('Full Name', '')}\n"
        f"Business Name: {row.get('Business Name', '')}\n"
        f"Email: {row.get('Email', '')}\n"
        f"Website URL: {row.get('Website URL', '')}\n"
        f"Primary Structural Concern: {row.get('Primary Structural Concern', '')}\n"
        f"Lead Source: {row.get('Lead Source', '')}\n"
    )


def parse_agent_output(parsed):
    agent_score = str(parsed.get("Agent Score", "")).strip()
    rationale = str(parsed.get("Rationale", "")).strip()
    proposed_tier = str(parsed.get("Proposed Tier", "")).strip()
    crm_status_new = str(
        parsed.get("CRM status")
        or parsed.get("CRM Status")
        or ""
    ).strip()
    followup_draft = str(parsed.get("Follow-up Draft", "")).strip()

    required_fields = {
        "Agent Score": agent_score,
        "Rationale": rationale,
        "Proposed Tier": proposed_tier,
        "CRM Status": crm_status_new,
        "Follow-up Draft": followup_draft,
    }

    missing_fields = [key for key, value in required_fields.items() if not value]
    if missing_fields:
        raise ValueError(f"Missing required fields in agent output: {missing_fields}")

    if crm_status_new not in AGENT_ALLOWED_FINAL_STATUSES:
        raise ValueError(
            f"Invalid CRM status from model: '{crm_status_new}'. "
            f"Must be one of: {sorted(AGENT_ALLOWED_FINAL_STATUSES)}"
        )

    return agent_score, rationale, proposed_tier, crm_status_new, followup_draft


def process_sheet():
    if not ANTHROPIC_KEY:
        raise EnvironmentError("Missing ANTHROPIC_API_KEY in environment.")

    sh = gc.open_by_key(SHEET_ID)
    worksheet = sh.sheet1
    rows = worksheet.get_all_records()

    print(f"Scanning {len(rows)} rows...")
    processed_count = 0

    for idx, row in enumerate(rows, start=2):
        if not should_process_row(row):
            continue

        crm_status_current = get_row_crm_status(row)

        if WRITE_NEW_STATUS_IF_BLANK and crm_status_current == "":
            worksheet.update(f"K{idx}", [["New"]])
            crm_status_current = "New"

        lead_text = build_lead_text(row)
        full_name = row.get("Full Name", "Unknown Lead")
        print(f"-> Analyzing Row {idx}: {full_name}")

        try:
            raw_response = analyze_lead(lead_text)
            parsed = extract_json_from_response(raw_response)

            (
                agent_score,
                rationale,
                proposed_tier,
                crm_status_new,
                followup_draft,
            ) = parse_agent_output(parsed)

            current_status_cell = normalize_crm_status(
                str(worksheet.acell(f"K{idx}").value or "")
            )
            if current_status_cell in PROTECTED_STATUSES:
                print(f"   Skipped update: row moved to '{current_status_cell}' during run.")
                continue

            update_values = [[
                agent_score,
                rationale,
                proposed_tier,
                crm_status_new,
                followup_draft,
            ]]

            worksheet.update(f"H{idx}:L{idx}", update_values)
            processed_count += 1
            print(f"   Success: updated row {idx}")

        except Exception as e:
            print(f"   Error on row {idx}: {e}")

    print(f"Done. Processed {processed_count} leads.")


if __name__ == "__main__":
    process_sheet()