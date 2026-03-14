PORTAL_URL = "http://planning.hackney.gov.uk/Northgate/PlanningExplorer/generalsearch.aspx"

TASK = f"""
Go to {PORTAL_URL}

Your goal is to find and save recent planning applications from Hackney Council.
This is a Northgate PlanningExplorer portal.

1. You should see a search form. Look for date fields — search for applications
   validated or received in the last 30 days.
2. If the form has a "Date Type" dropdown, select "Received" or "Validated".
   Set the date range to the last 30 days.
3. Click the Search button.
4. You should see a results list. For each application in the list, click into
   its detail page.
5. Extract all available fields: reference number, address, description of proposed
   work, application type, application status, key dates (received, validated, decided,
   target date), applicant name, agent name, ward, case officer, and the URL of the
   detail page.
6. Call save_application for EACH application. Put the core fields in the named
   parameters. Put ALL other fields (application type, ward, case officer, agent,
   target date, etc.) into raw_fields as a dict.
7. After saving, go back to the results list and continue with the next application.
8. If there are multiple pages of results, navigate through them.
9. Process at least 20 applications, or all available if fewer than 20.

Important:
- If a field is not available, pass an empty string "".
- Always set council to "hackney".
- Northgate forms often use ASP.NET postbacks — wait for page loads after clicking.
- If you encounter an error, try go_back and retry the search.
"""
