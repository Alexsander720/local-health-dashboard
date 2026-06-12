# Security Policy

## Scope

This project processes highly sensitive health information. Reports involving
data exposure, cross-origin access, unsafe persistence, credential handling, or
model-output injection are treated as security issues.

## Reporting

Do not open a public issue containing real health data, credentials, cookies,
device identifiers, or exploit details.

Until a dedicated private reporting channel is published, open a minimal issue
stating that you found a security problem and request private contact with the
maintainer. Include no sensitive reproduction data in that issue.

## Safe Defaults

- The server binds to `127.0.0.1`.
- Public demo mode uses synthetic data and rejects mutations.
- Local data, credentials, caches, and generated dashboards are ignored by Git.
- AI-generated HTML is sanitized before insertion.

## Unsupported Deployment

Running the server on a public network interface without authentication and TLS
is unsupported.
