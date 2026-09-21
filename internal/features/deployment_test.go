package features

import "testing"

// TestDeploymentRequiresTrue keeps Go entrypoints aligned with Python's opt-in values.
func TestDeploymentRequiresTrue(t *testing.T) {
	for _, value := range []string{"", "false", "FALSE", "0", "1", "yes", "tru", "true", " TRUE "} {
		t.Run(value, func(t *testing.T) {
			t.Setenv(DeploymentFlag, value)
			want := value == "true" || value == " TRUE "
			if DeploymentEnabled() != want || (RequireDeployment() == nil) != want {
				t.Fatalf("unexpected deployment availability for %q", value)
			}
		})
	}
}
