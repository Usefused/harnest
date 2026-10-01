Feature: agent_refund
Version: 1
Description: Require verified payment and a fresh Harnest approval for each refund attempt.

Rule: Verify payment
  When step "payment_verified" is submitted
  Then owner must be "orders"
  And this step is an entry point

Rule: Record Harnest approval
  When step "approval" is submitted
  Then owner must be "processor"
  And step "payment_verified" must have succeeded

Rule: Issue refund
  When step "refund_issued" is submitted
  Then owner must be "processor"
  And step "approval" must succeed before each invocation

Rule: Complete refund
  When step "refund_complete" is submitted
  Then owner must be "processor"
  And step "refund_issued" must have succeeded
  And this step is terminal
