---
name: incident-triage
description: Triage customer-reported service incidents when production impact, errors, authentication failures, or integrations are mentioned.
---

# Incident triage

Use this procedure when a customer reports a service incident. It produces a
recommendation only; the host application, not the agent, owns ticket creation.

1. Separate observed symptoms from assumptions. Never invent logs, causes, or
   recovery times.
2. Determine whether the customer explicitly says production is blocked. Do not
   infer a production block merely from urgency or frustration.
3. Call `triage_request` with a concise symptom summary and the confirmed
   `production_blocked` value.
4. Report the returned queue and priority, then ask for the smallest useful
   missing diagnostic detail, such as an error code or request ID.
5. Delegate root-cause investigation to `technical_specialist` when the request
   involves an API, integration, authentication, or reproducible error.

Do not request passwords, API keys, session cookies, or full authentication
headers. Ask customers to redact credentials from diagnostic material.
