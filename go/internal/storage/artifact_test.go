package storage

import (
	"testing"

	"github.com/google/uuid"
)

func TestParseArtifactKey(t *testing.T) {
	consultation := uuid.MustParse("3fa85f64-5717-4562-b3fc-2c963f66afa6")
	runConfig := uuid.MustParse("7c9e6679-7425-40de-944b-e07fc1f90ae7")
	key := "dev/consultations/3fa85f64-5717-4562-b3fc-2c963f66afa6/stages/nlp/7c9e6679-7425-40de-944b-e07fc1f90ae7/clinical_note.json"

	got, err := ParseArtifactKey(key)
	if err != nil {
		t.Fatalf("ParseArtifactKey: %v", err)
	}
	if got.Env != "dev" || got.Stage != "nlp" || got.Kind != "clinical_note" || got.Ext != "json" {
		t.Errorf("parsed the wrong parts: %+v", got)
	}
	if got.ConsultationID != consultation || got.RunConfigID != runConfig {
		t.Errorf("parsed the wrong ids: %+v", got)
	}
}

func TestParseArtifactKeyRejectsNonStageKeys(t *testing.T) {
	// Source audio lives outside the stages/ layout (decision #36: it is
	// recorded on the consultations row, not in artifacts). The retention
	// sweep relies on this rejection to skip it — parsing it as a stage
	// artifact would make every source recording look unreferenced and
	// delete the one immutable input every ablation arm shares.
	for name, key := range map[string]string{
		"source audio":     "dev/consultations/3fa85f64-5717-4562-b3fc-2c963f66afa6/source/audio/sha256-9f2a.wav",
		"consent":          "dev/consultations/3fa85f64-5717-4562-b3fc-2c963f66afa6/source/consent/abc.json",
		"eval output":      "dev/eval/b2f1a4d0-9c3e-4b7a-8f2d-1e6a9d5c7f31/got_k2/3fa85f64-5717-4562-b3fc-2c963f66afa6/metrics.json",
		"too few segments": "dev/consultations/x/stages/nlp/note.json",
		"empty":            "",
	} {
		if _, err := ParseArtifactKey(key); err == nil {
			t.Errorf("%s key %q must not parse as a stage artifact", name, key)
		}
	}
}

func TestParseArtifactKeyRejectsAnUnknownKind(t *testing.T) {
	// §3.3: artifact_kind is drawn from a closed enum. A worker inventing
	// one should fail here with a readable message rather than at the
	// artifacts.kind CHECK constraint.
	key := "dev/consultations/3fa85f64-5717-4562-b3fc-2c963f66afa6/stages/nlp/7c9e6679-7425-40de-944b-e07fc1f90ae7/scratchpad.json"
	if _, err := ParseArtifactKey(key); err == nil {
		t.Error("an out-of-enum artifact kind must be rejected")
	}
}

func TestStageArtifactKeyRoundTrips(t *testing.T) {
	c := &Client{Env: "test", Bucket: "coda-test"}
	consultation, runConfig := uuid.New(), uuid.New()
	key := c.StageArtifactKey(consultation, runConfig, "asr", "transcript", "json")

	got, err := ParseArtifactKey(key)
	if err != nil {
		t.Fatalf("a key this package built must parse: %q: %v", key, err)
	}
	if got.ConsultationID != consultation || got.RunConfigID != runConfig || got.Stage != "asr" || got.Kind != "transcript" {
		t.Errorf("round trip lost information: %+v", got)
	}
}

func TestConsultationPrefixCoversStageAndSourceKeys(t *testing.T) {
	// The erasure/retention sweep deletes under this prefix, so it must
	// cover both the stage outputs and the source audio.
	c := &Client{Env: "dev", Bucket: "coda"}
	id := uuid.New()
	prefix := c.ConsultationPrefix(id)

	for _, key := range []string{
		c.StageArtifactKey(id, uuid.New(), "nlp", "clinical_note", "json"),
		c.SourceAudioKey(id, "9f2a", "wav"),
	} {
		if len(key) <= len(prefix) || key[:len(prefix)] != prefix {
			t.Errorf("key %q is not under the consultation prefix %q", key, prefix)
		}
	}
}
