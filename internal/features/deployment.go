// Package features owns process-level opt-ins for unreleased functionality.
package features

import (
	"errors"
	"os"
	"strings"
)

const DeploymentFlag = "HARNEST_ENABLE_DEPLOYMENT"
const DeploymentDisabled = "Deployment is disabled. Set HARNEST_ENABLE_DEPLOYMENT=true to enable it."

// DeploymentEnabled requires explicit true; absent and invalid values stay off.
func DeploymentEnabled() bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv(DeploymentFlag)), "true")
}

// RequireDeployment rejects work before invoking a compiler or deployment backend.
func RequireDeployment() error {
	if !DeploymentEnabled() {
		return errors.New(DeploymentDisabled)
	}
	return nil
}
