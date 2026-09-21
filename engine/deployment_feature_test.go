package engine

import (
	"context"
	"strings"
	"testing"
)

// TestDeploymentDisabled blocks public pipeline and direct process adapter entrypoints.
func TestDeploymentDisabled(t *testing.T) {
	t.Setenv("HARNEST_ENABLE_DEPLOYMENT", "")
	deployer := &recordingDeployer{}
	for _, err := range []error{
		DeployAll(context.Background(), DeploymentPlan{}, deployer),
		CompileAndDeployAll(context.Background(), DeploymentPlan{}, PythonCompiler{}, deployer),
		(CommandDeployer{Command: "/missing/deployer"}).Deploy(context.Background(), Bundle{}),
	} {
		if err == nil || !strings.Contains(err.Error(), "HARNEST_ENABLE_DEPLOYMENT=true") {
			t.Fatalf("expected feature error before validation or execution, got %v", err)
		}
	}
}
