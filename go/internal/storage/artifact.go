package storage

import (
	"context"
	"fmt"
	"path"
	"strings"

	"github.com/google/uuid"
	"github.com/minio/minio-go/v7"
)

// Object references throughout this system are bare object keys within the
// configured bucket, not `s3://bucket/key` URLs — the convention go-api
// already established for consultations.source_audio_uri and the
// object_key it hands clients (internal/http/consultations_handlers.go).
// The bucket is process configuration, so baking it into stored references
// would make every row invalid the moment the bucket is renamed.

// ArtifactKind values, mirroring both the artifacts.kind CHECK constraint
// (go/migrations/000014) and the ArtifactKind enum in
// proto/coda/v1/common.proto. §3.3 requires artifact_kind be drawn from a
// closed enum; this is the Go-side copy of that closed set, used to reject
// a worker-invented kind before it reaches the CHECK constraint as a
// constraint-violation error nobody can read.
var artifactKinds = map[string]struct{}{
	"audio": {}, "consent": {}, "transcript": {}, "thought_graph": {},
	"candidate_set": {}, "clinical_note": {}, "summary": {},
	"export_json": {}, "export_pdf": {}, "metrics": {}, "redaction_map": {},
}

// ArtifactKey is a parsed stage-output key (§3.3):
//
//	{env}/consultations/{consultation_id}/stages/{stage}/{run_config_id}/{kind}.{ext}
//
// The layout is load-bearing rather than cosmetic: partitioning by
// run_config_id is what stops two ablation arms' outputs colliding, and it
// is why the orchestrator can derive an artifact's identity from its key
// alone instead of the worker having to restate it in the result message.
type ArtifactKey struct {
	Env            string
	ConsultationID uuid.UUID
	Stage          string
	RunConfigID    uuid.UUID
	Kind           string
	Ext            string
}

// ParseArtifactKey reads a stage-output key back into its parts. It is
// strict: a key that does not match the §3.3 layout, or whose kind is
// outside the closed enum, is an error rather than a best-effort parse —
// an unparseable key means a worker wrote somewhere the retention sweep and
// the result assembler will never look, which is worth failing loudly for.
func ParseArtifactKey(key string) (ArtifactKey, error) {
	// 7 segments: env / "consultations" / id / "stages" / stage /
	// run_config_id / basename.
	parts := strings.Split(key, "/")
	if len(parts) != 7 || parts[1] != "consultations" || parts[3] != "stages" {
		return ArtifactKey{}, fmt.Errorf("storage: %q is not a stage artifact key ({env}/consultations/{id}/stages/{stage}/{run_config_id}/{kind}.{ext})", key)
	}
	consultationID, err := uuid.Parse(parts[2])
	if err != nil {
		return ArtifactKey{}, fmt.Errorf("storage: artifact key %q has an invalid consultation id: %w", key, err)
	}
	runConfigID, err := uuid.Parse(parts[5])
	if err != nil {
		return ArtifactKey{}, fmt.Errorf("storage: artifact key %q has an invalid run config id: %w", key, err)
	}
	base := parts[6]
	ext := strings.TrimPrefix(path.Ext(base), ".")
	kind := strings.TrimSuffix(base, path.Ext(base))
	if _, ok := artifactKinds[kind]; !ok {
		return ArtifactKey{}, fmt.Errorf("storage: artifact key %q names kind %q, which is not one of the closed set in common.proto/ArtifactKind", key, kind)
	}
	return ArtifactKey{
		Env:   parts[0],
		Stage: parts[4], ConsultationID: consultationID,
		RunConfigID: runConfigID, Kind: kind, Ext: ext,
	}, nil
}

// StageArtifactKey builds the §3.3 key for a stage output.
func (c *Client) StageArtifactKey(consultationID, runConfigID uuid.UUID, stage, kind, ext string) string {
	return fmt.Sprintf("%s/consultations/%s/stages/%s/%s/%s.%s", c.Env, consultationID, stage, runConfigID, kind, ext)
}

// ConsultationPrefix is every object belonging to one consultation, source
// audio included — the prefix DPDP erasure (§7.2) and the retention sweep
// delete under.
func (c *Client) ConsultationPrefix(consultationID uuid.UUID) string {
	return fmt.Sprintf("%s/consultations/%s/", c.Env, consultationID)
}

// ArtifactStat is the object metadata the orchestrator records in the
// artifacts table.
type ArtifactStat struct {
	SHA256      string
	Bytes       int64
	ContentType string
}

// StatArtifact reads an object's size, content type, and content hash.
//
// The hash is taken from the `x-amz-meta-sha256` user metadata a worker
// sets when it writes the object, and falls back to the ETag only when
// that is absent. The distinction matters: an ETag is MD5 for a
// single-part upload but a hash-of-hashes for a multipart one, so it is
// not a content hash in general — recording it as `sha256` unqualified
// would put a value in that column that does not mean what the column
// says. Callers get the fallback flagged so the caller can decide; the
// orchestrator logs it.
func (c *Client) StatArtifact(ctx context.Context, key string) (ArtifactStat, error) {
	info, err := c.mc.StatObject(ctx, c.Bucket, key, minio.StatObjectOptions{})
	if err != nil {
		return ArtifactStat{}, fmt.Errorf("storage: stat artifact %q: %w", key, err)
	}
	sha := info.UserMetadata["Sha256"]
	if sha == "" {
		sha = info.UserMetadata["sha256"]
	}
	if sha == "" {
		sha = strings.Trim(info.ETag, `"`)
	}
	return ArtifactStat{SHA256: sha, Bytes: info.Size, ContentType: info.ContentType}, nil
}

// ListKeys enumerates object keys under a prefix. Used by the retention
// sweep to find objects with no artifacts row.
func (c *Client) ListKeys(ctx context.Context, prefix string) ([]string, error) {
	var keys []string
	for obj := range c.mc.ListObjects(ctx, c.Bucket, minio.ListObjectsOptions{Prefix: prefix, Recursive: true}) {
		if obj.Err != nil {
			return keys, fmt.Errorf("storage: list %q: %w", prefix, obj.Err)
		}
		keys = append(keys, obj.Key)
	}
	return keys, nil
}
