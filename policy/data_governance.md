# Data governance

> **Day-0 prerequisite.** No agent is used on project code until the data owner has
> filled in and signed section 4. This is not optional and not a later task.

## 1. Confidentiality classes and allowed AI services

The classes below are a template; map them to the organisation's own scheme.

| Class | Examples | Allowed AI service |
|---|---|---|
| Public | Open-source code, public docs | Any approved service |
| Internal | Non-customer-specific tooling, build scripts, generic utilities | Approved service with contract terms accepted by the data owner |
| Confidential | Product source code, test data, requirement text | Only services with a signed enterprise agreement, a documented retention setting (zero data retention where offered) and no training on inputs |
| Customer-restricted | Anything covered by a customer contract, NDA, or export or TISAX-style label | Only if the customer contract explicitly permits the service; otherwise **no AI service**, local or otherwise |
| Secret | Keys, credentials, personal data, vehicle identifiers | Never sent to any AI service; blocked by `secret_pii_check` |

## 2. Contract and technical checks (before enabling a service)

1. Retention: what is stored, for how long, who can read it. Is zero data retention
   available and configured for this account? (external, verify:
   https://platform.claude.com/docs/en/manage-claude/api-and-data-retention)
2. Training: confirm inputs and outputs are not used for training under the contract.
   (external, verify: https://www.anthropic.com/legal/commercial-terms)
3. Data location and sub-processors acceptable to the organisation.
4. Third-party-code matching/IP indemnity terms reviewed by legal.
5. Security labels such as TISAX are respected (external, verify: https://www.enx.com/tisax/).
6. **Customer contract check:** for every customer whose data or code is in the
   repository, the contract and its cybersecurity interface agreement are read for clauses
   on third-party tools, cloud processing, subcontractors and AI. Result: allowed /
   allowed with notice / forbidden. Forbidden means the repository part is excluded from
   AI use via path rules.
7. Regulatory transparency obligations that may apply to the organisation are checked by
   legal (external, verify:
   https://www.morganlewis.com/blogs/sourcingatmorganlewis/2026/08/eu-ai-acts-transparency-rules-what-went-into-effect-on-2-august).

## 3. Technical controls

- Path allow/deny list: which directories may be placed in agent context.
- `enforcement/checks/secret_pii_check.py` runs as pre-commit and in CI.
- Agent context is limited to the task's files plus the pack knowledge.
- Prompts and outputs are logged internally under the evidence directory with the same
  classification as the source.

## 4. Sign-off record (fill in on day 0)

| Item | Value |
|---|---|
| Data owner (name/role) | |
| Approved service(s) and account | |
| Highest class allowed | |
| Retention setting verified on (date) | |
| Customer contract check done on (date), result | |
| Excluded paths | |
| Next review date | |

## 5. Disclosure

Recording AI use internally (`provenance.md`) is separate from telling a customer.
Disclosure is an owner decision documented here.
