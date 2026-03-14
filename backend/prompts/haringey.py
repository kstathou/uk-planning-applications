PORTAL_URL = "http://www.planningservices.haringey.gov.uk/portal/"

TASK = f"""
Go to {PORTAL_URL}

Your goal is to find and save recent planning applications from Haringey Council.

1. Look for a search page or weekly list of applications.
2. If there is a search form, search for applications from the last 30 days.
   If there is a weekly list or recent applications page, use that instead.
3. For each application found, click into its detail page.
4. Extract all available fields: reference number, address, description of proposed
   work, application status, key dates (submitted, validated, decided), applicant name,
   and the URL of the detail page.
5. Call save_application for EACH application. Put the core fields in the named
   parameters. Put ALL other fields you find on the page into raw_fields as a dict.
6. After saving, go back to the list and continue with the next application.
7. Process at least 20 applications, or all available if fewer than 20.

Important:
- If a field is not available, pass an empty string "".
- Always set council to "haringey".
- If you encounter a CAPTCHA or error page, try refreshing or navigating back.
- If the page uses frames or iframes, look inside them for content.
"""
