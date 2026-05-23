# Required GitHub Labels

The issue templates in `.github/ISSUE_TEMPLATE/` declare a `labels:` field on
each form. GitHub's behavior for that field: a label is only attached to the
new issue if it **already exists in the repository**. Missing labels are
silently dropped — they are NOT auto-created.

The "Process trade-log / chat / rec-action / idea-action issue" workflow
(`process_trade.yml`) gates its job on those labels. When the labels do not
exist, every chat / trade / rec / idea-action issue is created unlabeled,
the workflow's `if:` guard rejects it, and the user's submission is silently
dropped — a data-loss bug.

To prevent this, the labels below are provisioned on the repository. The
durable source of truth is `.github/labels.yml`; the `sync_labels.yml`
workflow reconciles the repo with that file on push and on demand.

## Labels

| Name          | Color    | Description                                              |
| ------------- | -------- | -------------------------------------------------------- |
| `chat`        | `1f6feb` | Chat panel message from the dashboard.                   |
| `trade-log`   | `8957e5` | Manual trade log entry.                                  |
| `rec-action`  | `2da44e` | Accept / reject / counter on a recommendation.           |
| `idea-action` | `bf8700` | Verdict on a candidate idea.                             |

## How they get provisioned

1. `.github/labels.yml` defines them.
2. `.github/workflows/sync_labels.yml` creates/updates them on the repo when
   that file changes on `main`, or when manually dispatched.
3. `tests/test_issue_template_labels.py` asserts every label referenced by an
   issue template has a definition in `labels.yml`, so a new template can't
   reintroduce this bug.
