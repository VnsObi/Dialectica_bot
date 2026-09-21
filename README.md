# Dialectica agent

Dialectica agent is a Python service for researching prediction-market claims and, when configured for a private production environment, executing guarded on-chain actions. It is not a chatbot wrapper: it combines claim lifecycle monitoring, structured LLM interpretation, evidence retrieval, stateful persistence, and transaction execution.

This public repository is a proof-of-work release. Protocol endpoints, contract identifiers, credentials, wallet material, local databases, and operational logs are intentionally not included.

## Problem and approach

The agent turns an open-ended market claim into a constrained execution workflow:

1. Poll active claims and map API data to a typed internal model.
2. Interpret price and factual claims into structured predicates with an LLM.
3. Retrieve price data or web-search evidence; factual claims are adjudicated by an LLM using retrieved snippets (RAG).
4. Produce `TRUE`, `FALSE`, or `NO_BET` with confidence. Ambiguous, unsupported, or failed evaluations fail to `NO_BET`.
5. Verify on-chain eligibility before a transaction, encrypt the vote, manage allowance/gas/nonce details, then persist the action.
6. Track challenge and settlement transitions locally and attempt guarded challenge, payout, or refund actions where the protocol permits them.

## Architecture

`src/main.py` coordinates the workflow. The principal modules are:

- `ingestion/`: asynchronous claim API client and typed claim mapping.
- `interpretation/`: structured LLM claim parser.
- `research/`: CoinGecko price lookup and Tavily evidence retrieval.
- `decision/`: deterministic numeric comparisons plus LLM evidence adjudication.
- `execution/`: wallet signing, RSA-OAEP vote encryption, contract-revert handling, preflight guards, Multicall3 reads, and settlement polling.
- `utils/`: environment/configuration, SQLAlchemy persistence, domain models, and optional alerts.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for flows, boundaries, and state transitions.

## Execution and reliability safeguards

- **Claim monitoring:** separate active-claim and settlement/challenge polling paths retain lifecycle state in SQLAlchemy.
- **Decision guardrails:** unavailable evidence, invalid structured output, or LLM errors return `NO_BET` rather than an execution recommendation.
- **Wallet operations:** private keys are read only from the runtime environment and are never logged or committed.
- **Gas and nonce handling:** transaction builders use pending nonces, EIP-1559 fields, gas estimation buffers, and receipt checks. Batch payout processing allocates sequential pending nonces.
- **Transaction safeguards:** contract-state preflight checks run before execution; Multicall3 batches read-heavy checks. Missing/unavailable preflight data now fails closed.
- **Persistence:** claims, decisions, actions, and failed transaction metadata are retained locally; successful payout/refund processing clears completed local tracking records.
- **Failure handling:** API/LLM calls use bounded retries where implemented; custom contract errors inform expiry, circuit-breaker, and audit behavior. The agent continues its poll loop after a failed cycle.

## Why this project matters

The code demonstrates production AI-agent engineering beyond prompt orchestration: structured LLM interpretation and RAG evidence review feed stateful workflows that call external APIs and prepare financially sensitive on-chain operations. It includes execution guardrails, transaction failure paths, persistence, background lifecycle monitoring, and container/deployment configuration. The public version deliberately leaves protocol-specific production configuration private.

## Local setup (safe by default)

Requirements: Python 3.11+ is recommended.

```bash
cd dialectica-agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python src/main.py --once
```

The example environment uses `MOCK_MODE=true`, so the local smoke run does not require credentials and should not perform real protocol operations. Do not switch to production mode without an independent operational review, a dedicated funded wallet, reviewed contract addresses, endpoint access, spending limits, and monitoring.

## Environment variables

`.env.example` is the authoritative list. `PRIVATE_KEY`, `OPENAI_API_KEY`, `TAVILY_API_KEY`, RPC URLs, webhook URLs, database URLs, protocol API URLs, and contract identifiers are all sensitive runtime configuration and must remain outside Git.

When `MOCK_MODE=false`, configuration validation requires a private key, RPC URL, protocol API URL, and the relevant contract identifiers. Public defaults are intentionally absent.

## Testing and checks

From `dialectica-agent/`:

```bash
pytest -q
python -m compileall -q src tests
```

For dependency and secret checks, install the optional tools in an isolated environment and run `pip-audit -r requirements.txt` and a repository/history secret scanner such as Gitleaks. Do not use a production `.env` for tests or scans that may upload data.

## Deployment

No production deployment configuration is shipped in this public release. Run the service from `dialectica-agent/` in an isolated Python or container runtime; inject secrets through the deployment platform’s secret store, persist the database outside ephemeral storage if durability is required, restrict outbound network access to approved providers, and use a dedicated low-balance execution wallet. No production infrastructure is configured or modified by this repository.

## Limitations

- The public release cannot run against the original private protocol environment without configuration supplied by an authorized operator.
- LLM and external evidence quality are not guarantees of market correctness; `NO_BET` is an expected outcome.
- Smart-contract ABIs and lifecycle assumptions must be independently verified against the deployed protocol before any real execution.
- SQLite is appropriate for local development; production concurrency and durability requirements may call for managed Postgres and migration tooling.
- This is engineering evidence, not financial advice or a recommendation to deploy an autonomous trading system.

## License

Released under the [MIT License](LICENSE). Third-party packages retain their own licenses.
