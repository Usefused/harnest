/** Only the authenticated server response can enable deployment controls. */
export function deploymentEnabled(workspace) {
  return workspace?.features?.deployment === true;
}

/** Keep deployment invisible before loading workspace metadata and when disabled. */
export function renderDeploymentControl(control,workspace,project) {
  control.hidden=!deploymentEnabled(workspace);
  control.disabled=control.hidden || !project;
}
