package main

import (
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"
)

const (
	maxTemplateServices           = 8
	maxTemplateServicePorts       = 64
	maxTemplateServiceEnvironment = 64
	maxTemplateServiceProvides    = 64
	maxTemplateServiceVolumes     = 64
	maxTemplateServiceDependsOn   = 64
)

var (
	templateServiceNamePattern = regexp.MustCompile(`^[a-z][a-z0-9-]{0,62}$`)
	templateEnvNamePattern     = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	templatePortPattern        = regexp.MustCompile(`^[0-9]{1,5}$`)
	templateDigestPattern      = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// validateTemplateServices applies the service contract once, before rendering or packaging.
func validateTemplateServices(services []templateService) error {
	if len(services) > maxTemplateServices {
		return fmt.Errorf("template declares too many services")
	}
	names := map[string]bool{}
	provides := map[string]bool{}
	for _, service := range services {
		if err := validateTemplateService(service, names, provides); err != nil {
			return err
		}
	}
	return validateTemplateServiceDependencies(services, names)
}

// validateTemplateService checks one service declaration against its shared namespaces.
func validateTemplateService(service templateService, names, provides map[string]bool) error {
	if err := validateTemplateServiceName(service, names); err != nil {
		return err
	}
	if err := validateTemplateServiceImage(service.Image); err != nil {
		return err
	}
	if err := validateTemplateServicePorts(service.Ports); err != nil {
		return err
	}
	if err := validateTemplateServiceEnvironment(service.Environment); err != nil {
		return err
	}
	if err := validateTemplateServiceHealthcheck(service.Healthcheck); err != nil {
		return err
	}
	if err := validateTemplateServiceVolumes(service.Volumes); err != nil {
		return err
	}
	if err := validateTemplateServiceDependsOn(service.DependsOn); err != nil {
		return err
	}
	return validateTemplateServiceProvides(service.Provides, provides)
}

func validateTemplateServiceName(service templateService, names map[string]bool) error {
	if !templateServiceNamePattern.MatchString(service.Name) {
		return fmt.Errorf("template service has an invalid name %q", service.Name)
	}
	if names[service.Name] {
		return fmt.Errorf("template declares duplicate service %q", service.Name)
	}
	names[service.Name] = true
	return nil
}

func validateTemplateServiceImage(image string) error {
	if image == "" || len(image) > 300 || !safeExtensionMetadataValue(image, 300) {
		return fmt.Errorf("template service image is invalid")
	}
	_, digest, found := strings.Cut(image, "@sha256:")
	if !found || !templateDigestPattern.MatchString(digest) {
		return fmt.Errorf("template service image %q must be pinned with @sha256:<64 hex digits>", image)
	}
	return nil
}

func validateTemplateServicePorts(ports []string) error {
	if len(ports) > maxTemplateServicePorts {
		return fmt.Errorf("template service declares too many ports")
	}
	for _, port := range ports {
		if err := validateTemplateServicePort(port); err != nil {
			return err
		}
	}
	return nil
}

func validateTemplateServicePort(port string) error {
	if !templatePortPattern.MatchString(port) {
		return fmt.Errorf("template service port %q must be a container port number", port)
	}
	number, err := strconv.Atoi(port)
	if err != nil || number < 1 || number > 65535 {
		return fmt.Errorf("template service port %q is out of range", port)
	}
	return nil
}

func validateTemplateServiceEnvironment(environment map[string]string) error {
	if len(environment) > maxTemplateServiceEnvironment {
		return fmt.Errorf("template service declares too many environment variables")
	}
	for key, value := range environment {
		if !templateEnvNamePattern.MatchString(key) {
			return fmt.Errorf("template service environment has an invalid name %q", key)
		}
		if !safeExtensionMetadataValue(value, 2_000) {
			return fmt.Errorf("template service environment value for %q is invalid", key)
		}
	}
	return nil
}

func validateTemplateServiceHealthcheck(healthcheck *templateHealthcheck) error {
	if healthcheck == nil {
		return nil
	}
	if len(healthcheck.Test) == 0 || len(healthcheck.Test) > 32 {
		return fmt.Errorf("template service healthcheck test must have between 1 and 32 parts")
	}
	for _, part := range healthcheck.Test {
		if !safeExtensionMetadataValue(part, 500) {
			return fmt.Errorf("template service healthcheck test is invalid")
		}
	}
	if healthcheck.Retries < 0 {
		return fmt.Errorf("template service healthcheck retries cannot be negative")
	}
	return validateTemplateServiceHealthcheckTiming(healthcheck.Interval, healthcheck.Timeout)
}

func validateTemplateServiceHealthcheckTiming(interval, timeout string) error {
	for label, value := range map[string]string{"interval": interval, "timeout": timeout} {
		if value != "" && !safeExtensionMetadataValue(value, 20) {
			return fmt.Errorf("template service healthcheck %s is invalid", label)
		}
	}
	return nil
}

func validateTemplateServiceVolumes(volumes []string) error {
	if len(volumes) > maxTemplateServiceVolumes {
		return fmt.Errorf("template service declares too many volumes")
	}
	for _, volume := range volumes {
		if !safeExtensionMetadataValue(volume, 500) {
			return fmt.Errorf("template service volume is invalid")
		}
	}
	return nil
}

func validateTemplateServiceDependsOn(dependencies []string) error {
	if len(dependencies) > maxTemplateServiceDependsOn {
		return fmt.Errorf("template service declares too many dependencies")
	}
	for _, dependency := range dependencies {
		if !templateServiceNamePattern.MatchString(dependency) {
			return fmt.Errorf("template service dependency has an invalid name %q", dependency)
		}
	}
	return nil
}

func validateTemplateServiceProvides(provides map[string]string, seen map[string]bool) error {
	if len(provides) > maxTemplateServiceProvides {
		return fmt.Errorf("template service declares too many provided values")
	}
	for key, value := range provides {
		if !templateEnvNamePattern.MatchString(key) {
			return fmt.Errorf("template service provides an invalid environment name %q", key)
		}
		if seen[key] {
			return fmt.Errorf("template declares duplicate provided environment %q", key)
		}
		seen[key] = true
		if !safeExtensionMetadataValue(value, 2_000) {
			return fmt.Errorf("template service provided value for %q is invalid", key)
		}
	}
	return nil
}

func validateTemplateServiceDependencies(services []templateService, names map[string]bool) error {
	for _, service := range services {
		for _, dependency := range service.DependsOn {
			if !names[dependency] {
				return fmt.Errorf("template service %q depends on unknown service %q", service.Name, dependency)
			}
		}
	}
	return nil
}

// applyTemplateServices injects provided URLs and renders docker-compose.yml.
func applyTemplateServices(resources map[string][]byte, projectName string, services []templateService) error {
	if len(services) == 0 {
		return nil
	}
	provides := mergedTemplateProvides(services)
	if len(provides) > 0 {
		config, ok := resources["config.yaml"]
		if !ok {
			return fmt.Errorf("template services require config.yaml")
		}
		injected, err := injectConfigEnvironment(config, provides)
		if err != nil {
			return err
		}
		resources["config.yaml"] = injected
	}
	compose, err := renderComposeFile(projectName, services)
	if err != nil {
		return err
	}
	resources["docker-compose.yml"] = compose
	return nil
}

func mergedTemplateProvides(services []templateService) map[string]string {
	merged := map[string]string{}
	for _, service := range services {
		for key, value := range service.Provides {
			merged[key] = value
		}
	}
	return merged
}

type composeFile struct {
	Name     string                    `yaml:"name"`
	Services map[string]composeService `yaml:"services"`
}

type composeService struct {
	Image       string              `yaml:"image"`
	Command     []string            `yaml:"command,omitempty"`
	Restart     string              `yaml:"restart,omitempty"`
	Environment map[string]string   `yaml:"environment,omitempty"`
	Healthcheck *composeHealthcheck `yaml:"healthcheck,omitempty"`
	Ports       []string            `yaml:"ports,omitempty"`
	Volumes     []string            `yaml:"volumes,omitempty"`
	DependsOn   []string            `yaml:"depends_on,omitempty"`
}

type composeHealthcheck struct {
	Test     []string `yaml:"test"`
	Interval string   `yaml:"interval,omitempty"`
	Timeout  string   `yaml:"timeout,omitempty"`
	Retries  int      `yaml:"retries,omitempty"`
}

// renderComposeFile renders deterministic, localhost-only Compose output.
func renderComposeFile(slug string, services []templateService) ([]byte, error) {
	document := composeFile{
		Name:     "harnest-" + slug,
		Services: map[string]composeService{},
	}
	for _, service := range services {
		document.Services[service.Name] = renderComposeService(service)
	}
	contents, err := yaml.Marshal(document)
	if err != nil {
		return nil, fmt.Errorf("encode docker-compose.yml: %w", err)
	}
	return contents, nil
}

func renderComposeService(service templateService) composeService {
	rendered := composeService{
		Image:       service.Image,
		Command:     service.Command,
		Restart:     "unless-stopped",
		Environment: service.Environment,
		Ports:       renderComposePorts(service),
		Volumes:     service.Volumes,
		DependsOn:   service.DependsOn,
	}
	if service.Healthcheck != nil {
		rendered.Healthcheck = &composeHealthcheck{
			Test:     service.Healthcheck.Test,
			Interval: service.Healthcheck.Interval,
			Timeout:  service.Healthcheck.Timeout,
			Retries:  service.Healthcheck.Retries,
		}
	}
	return rendered
}

func renderComposePorts(service templateService) []string {
	variable := "HARNEST_" + strings.ToUpper(strings.ReplaceAll(service.Name, "-", "_")) + "_PORT"
	ports := make([]string, 0, len(service.Ports))
	for _, port := range service.Ports {
		ports = append(ports, fmt.Sprintf("127.0.0.1:${%s:-%s}:%s", variable, port, port))
	}
	return ports
}

// injectConfigEnvironment merges provided service URLs into config.yaml's environment mapping.
func injectConfigEnvironment(contents []byte, provides map[string]string) ([]byte, error) {
	var document yaml.Node
	if err := yaml.Unmarshal(contents, &document); err != nil {
		return nil, fmt.Errorf("invalid template config.yaml: %w", err)
	}
	root, err := documentMapping(&document)
	if err != nil {
		return nil, err
	}
	environment, err := ensureEnvironmentMapping(root)
	if err != nil {
		return nil, err
	}
	for _, key := range sortedStringKeys(provides) {
		setMappingScalar(environment, key, provides[key])
	}
	return yaml.Marshal(&document)
}

func documentMapping(document *yaml.Node) (*yaml.Node, error) {
	if document.Kind != yaml.DocumentNode || len(document.Content) != 1 {
		return nil, fmt.Errorf("invalid YAML document")
	}
	root := document.Content[0]
	if root.Kind != yaml.MappingNode {
		return nil, fmt.Errorf("config.yaml must be a mapping")
	}
	return root, nil
}

func ensureEnvironmentMapping(root *yaml.Node) (*yaml.Node, error) {
	spec, err := ensureMappingChild(root, "spec")
	if err != nil {
		return nil, err
	}
	return ensureMappingChild(spec, "environment")
}

func ensureMappingChild(parent *yaml.Node, key string) (*yaml.Node, error) {
	if parent.Kind != yaml.MappingNode {
		return nil, fmt.Errorf("config.yaml %s must be a mapping", key)
	}
	if child := mappingValue(parent, key); child != nil {
		if child.Kind == yaml.MappingNode {
			return child, nil
		}
		if child.Kind == yaml.ScalarNode && child.Tag == "!!null" {
			child.Kind = yaml.MappingNode
			child.Tag = "!!map"
			child.Value = ""
			return child, nil
		}
		return nil, fmt.Errorf("config.yaml %s must be a mapping", key)
	}
	keyNode := &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: key}
	valueNode := &yaml.Node{Kind: yaml.MappingNode, Tag: "!!map"}
	parent.Content = append(parent.Content, keyNode, valueNode)
	return valueNode, nil
}

func mappingValue(mapping *yaml.Node, key string) *yaml.Node {
	for index := 0; index+1 < len(mapping.Content); index += 2 {
		if mapping.Content[index].Value == key {
			return mapping.Content[index+1]
		}
	}
	return nil
}

func setMappingScalar(mapping *yaml.Node, key, value string) {
	if existing := mappingValue(mapping, key); existing != nil {
		existing.Kind = yaml.ScalarNode
		existing.Tag = "!!str"
		existing.Value = value
		return
	}
	keyNode := &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: key}
	valueNode := &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: value}
	mapping.Content = append(mapping.Content, keyNode, valueNode)
}

func sortedStringKeys(values map[string]string) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}
