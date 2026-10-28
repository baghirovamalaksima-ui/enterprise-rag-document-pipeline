# Security and operating boundaries

This is a portfolio reference implementation, not an audited production service.
Use synthetic/public documents only. No clinical efficacy, HIPAA/GDPR compliance,
penetration-test result, or general prompt-injection resistance is claimed.

API keys resolve to tenants server-side. Key storage is environment-based; there
is no identity provider, per-role authorization, key-rotation service, distributed
rate limiter, or encrypted application database. Deploy behind TLS and a gateway;
provision a secret manager, disk encryption, backups, retention controls and monitoring
before any real organizational deployment. Run one API process per SQLite volume;
never use a shared network filesystem for SQLite WAL. Tenant isolation does not
protect against a privileged operator with filesystem access.

OpenAI is opt-in and sends query/evidence to an external service. `store:false`
does not establish zero retention or regulatory compliance. Tests mock this integration.
Exact quotes establish attribution, not truth, relevance or safety. Rule-based guards
and PII detection have false positives and false negatives, especially obfuscated,
multilingual, or novel input. Do not submit real patient information.

Do not post secrets or confidential documents in public issues. Report a reproducible
issue without sensitive data through the repository owner's private reporting channel
if available. No private contact address is invented here.
