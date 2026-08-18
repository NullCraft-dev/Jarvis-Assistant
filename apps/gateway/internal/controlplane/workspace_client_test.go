package controlplane

import (
	"testing"
	"time"
)

func TestWorkspacePickerUsesDedicatedLongerTimeout(t *testing.T) {
	client := NewClient("http://control-plane")
	if client.httpClient.Timeout != 10*time.Second {
		t.Fatalf("ordinary timeout=%s", client.httpClient.Timeout)
	}
	if client.pickerHTTPClient.Timeout <= 60*time.Second {
		t.Fatalf("picker timeout must exceed macOS picker timeout: %s", client.pickerHTTPClient.Timeout)
	}
}
