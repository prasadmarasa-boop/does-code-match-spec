# Conference Demo Flow

1. Attendee scans QR code.
2. Public Streamlit app opens in the browser.
3. Choose:
   - SAS-only mode, or
   - SAS + specification mode.
4. Upload the provided synthetic sample files or paste a short SAS program.
5. Click **Analyze**.
6. Review:
   - final dataset
   - final variables
   - Assigned vs Derived classification
   - source lineage
   - derivation logic
   - confidence
7. If a specification is uploaded, review:
   - MATCH
   - MISMATCH
   - REVIEW REQUIRED
8. Demonstrate an intentionally incorrect spec row and show that the tool flags it.

## Public-demo safety

Use only synthetic/example code and specifications during the conference.
Display a prominent notice telling attendees not to upload sponsor-confidential code,
patient-level data, PHI, credentials, or proprietary study materials.
