---
title: Terraform module deployment and validation
type: repository
knowledge_level: pattern
pattern_key: terraform_cloud_run_module
evidence_status: confirmed_success
labels:
  - topic:terraform
  - topic:gcp
  - topic:cloud-run
  - topic:ci-cd
scope:
  organization: Acme Corp
  provider: GCP
  runtime: Cloud Run
  environment: staging
actions:
  - action_key: terraform_validate_and_plan
    canonical_action_key: terraform_plan
    confidence: 0.98
    subjects:
      - terraform module
    objects:
      - execution plan
    tools:
      - terraform fmt
      - terraform validate
      - terraform plan
    route: format check -> syntax validation -> state lock plan
    outcome: execution plan generated without errors
claims:
  - id: c1
    text: Running terraform fmt before validate catches syntax and styling issues prior to plan.
    claim_key: terraform_fmt_before_validate
    polarity: affirmed
    claim_type: tool_observation
    confidence: 0.99
    evidence: []
---

<!-- exocortex:managed:start -->
## Summary
Best practice pattern for testing and validating Terraform modules deploying to Cloud Run. Enforces formatting, provider validation, and plan generation before applying changes.

## Facts
- Running `terraform fmt -check` prevents unformatted code from entering CI/CD pipelines.
- Running `terraform validate` ensures provider schemas and variables are consistent.
- Execution plans must be reviewed before apply in staging and production environments.

## Actions
- terraform_validate_and_plan (tools: terraform fmt, terraform validate, terraform plan)
<!-- exocortex:managed:end -->
