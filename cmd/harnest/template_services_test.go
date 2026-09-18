package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const testDigest = "0000000000000000000000000000000000000000000000000000000000000000"

func manifestWithServices(services string) string {
	return "apiVersion: harnest.dev/v1alpha1\nkind: Template\nmetadata:\n  name: support\n" + services
}

func serviceTemplateWheel(t *testing.T) []byte {
	t.Helper()
	entries := defaultTemplateEntries()
	entries[testTemplateModule+"/harnest-template.yaml"] = []byte(manifestWithServices(
		"services:\n" +
			"  - name: postgres\n" +
			"    image: postgres:16-alpine@sha256:" + testDigest + "\n" +
			"    ports: [\"5432\"]\n" +
			"    environment:\n      POSTGRES_DB: harnest\n" +
			"    healthcheck:\n      test: [\"CMD-SHELL\", \"pg_isready -d harnest\"]\n" +
			"    provides:\n      DATABASE_URL: postgresql://postgres:postgres@postgres:5432/harnest\n",
	))
	return buildTemplateWheel(t, entries, nil)
}

func TestRenderComposeFile(t *testing.T) {
	services := []templateService{{
		Name:        "postgres",
		Image:       "postgres:16-alpine@sha256:" + testDigest,
		Ports:       []string{"5432"},
		Environment: map[string]string{"POSTGRES_DB": "harnest"},
		Healthcheck: &templateHealthcheck{Test: []string{"CMD-SHELL", "pg_isready -d harnest"}, Retries: 12},
	}}
	contents, err := renderComposeFile("support-agent", services)
	if err != nil {
		t.Fatal(err)
	}
	for _, expected := range []string{
		"name: harnest-support-agent",
		"image: postgres:16-alpine@sha256:" + testDigest,
		"127.0.0.1:${HARNEST_POSTGRES_PORT:-5432}:5432",
		"restart: unless-stopped",
		"pg_isready -d harnest",
	} {
		if !strings.Contains(string(contents), expected) {
			t.Fatalf("docker-compose.yml missing %q:\n%s", expected, contents)
		}
	}
}

func TestDecodeTemplateManifestRejectsInvalidServices(t *testing.T) {
	cases := []struct {
		name     string
		services string
		want     string
	}{
		{"unpinned image", "services:\n  - name: postgres\n    image: postgres:16\n", "sha256"},
		{"bad name", "services:\n  - name: Bad_Name\n    image: postgres:16@sha256:" + testDigest + "\n", "name"},
		{"bad port", "services:\n  - name: postgres\n    image: postgres:16@sha256:" + testDigest + "\n    ports: [\"0\"]\n", "port"},
		{"bad provides", "services:\n  - name: postgres\n    image: postgres:16@sha256:" + testDigest + "\n    provides:\n      BAD-NAME: x\n", "environment name"},
		{"unknown dependency", "services:\n  - name: app\n    image: postgres:16@sha256:" + testDigest + "\n    depends_on: [missing]\n", "depends on unknown"},
	}
	for _, testcase := range cases {
		t.Run(testcase.name, func(t *testing.T) {
			_, err := decodeTemplateManifest([]byte(manifestWithServices(testcase.services)))
			if err == nil || !strings.Contains(err.Error(), testcase.want) {
				t.Fatalf("decode error = %v, want %q", err, testcase.want)
			}
		})
	}
}

func TestInjectConfigEnvironment(t *testing.T) {
	config := []byte(
		"apiVersion: harnest.dev/v1alpha1\nkind: Agent\nmetadata:\n  name: agent\nspec:\n  environment:\n    OPENAI_MODEL: test\n",
	)
	provides := map[string]string{"DATABASE_URL": "postgresql://postgres@postgres:5432/db"}
	result, err := injectConfigEnvironment(config, provides)
	if err != nil {
		t.Fatal(err)
	}
	for _, expected := range []string{"DATABASE_URL: postgresql://postgres@postgres:5432/db", "OPENAI_MODEL: test"} {
		if !strings.Contains(string(result), expected) {
			t.Fatalf("injected config missing %q:\n%s", expected, result)
		}
	}
}

func TestApplyTemplateServicesWritesComposeAndEnvironment(t *testing.T) {
	config := []byte(
		"apiVersion: harnest.dev/v1alpha1\nkind: Agent\nmetadata:\n  name: agent\nspec:\n  framework:\n    name: adk\n",
	)
	resources := map[string][]byte{"config.yaml": config, "agent.py": []byte("x")}
	services := []templateService{{
		Name:     "postgres",
		Image:    "postgres:16-alpine@sha256:" + testDigest,
		Ports:    []string{"5432"},
		Provides: map[string]string{"DATABASE_URL": "postgresql://postgres@postgres:5432/db"},
	}}
	if err := applyTemplateServices(resources, "support-agent", services); err != nil {
		t.Fatal(err)
	}
	if _, ok := resources["docker-compose.yml"]; !ok {
		t.Fatal("docker-compose.yml was not generated")
	}
	if !strings.Contains(string(resources["config.yaml"]), "DATABASE_URL: postgresql://postgres@postgres:5432/db") {
		t.Fatalf("config.yaml missing injected URL:\n%s", resources["config.yaml"])
	}
}

func TestCreateScaffoldFromTemplateWritesComposeFile(t *testing.T) {
	server := templateWheelServer(t, serviceTemplateWheel(t))
	app := testTemplateApplication()
	target := filepath.Join(t.TempDir(), "my-agent")

	if _, err := app.createScaffoldFromTemplate(
		context.Background(), target, "my-agent", server.URL+"/support.whl", "",
	); err != nil {
		t.Fatal(err)
	}
	compose, err := os.ReadFile(filepath.Join(target, "docker-compose.yml"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(compose), "name: harnest-my-agent") ||
		!strings.Contains(string(compose), "image: postgres:16-alpine@sha256:"+testDigest) {
		t.Fatalf("unexpected docker-compose.yml:\n%s", compose)
	}
	config, err := os.ReadFile(filepath.Join(target, "config.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(config), "DATABASE_URL: postgresql://postgres:postgres@postgres:5432/harnest") {
		t.Fatalf("config.yaml missing injected URL:\n%s", config)
	}
}

func TestPackageTemplateCarriesServices(t *testing.T) {
	root := t.TempDir()
	writePackageFixture(t, root)
	authored := manifestWithServices(
		"services:\n  - name: redis\n    image: redis:7-alpine@sha256:" + testDigest + "\n    ports: [\"6379\"]\n",
	)
	if err := os.WriteFile(filepath.Join(root, templateManifestFilename), []byte(authored), 0o644); err != nil {
		t.Fatal(err)
	}
	output := filepath.Join(root, "out.whl")
	app := testTemplateApplication()
	if _, err := app.packageTemplate(root, "", "", output); err != nil {
		t.Fatal(err)
	}
	wheel, err := os.ReadFile(output)
	if err != nil {
		t.Fatal(err)
	}
	pkg, err := readTemplateWheelPackage(wheel)
	if err != nil {
		t.Fatal(err)
	}
	if len(pkg.Manifest.Services) != 1 || pkg.Manifest.Services[0].Name != "redis" {
		t.Fatalf("packaged services = %+v", pkg.Manifest.Services)
	}
}
