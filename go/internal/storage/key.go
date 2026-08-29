package storage

import (
	"fmt"
	"regexp"

	"github.com/google/uuid"
)

// AllowedAudioContentTypes is v1's upload allowlist (plan.md Phase 1: "Audio
// upload (WAV/MP3)"). The extension a content type maps to is baked into
// the object key so a MinIO key alone tells you the format without a HEAD
// request.
var AllowedAudioContentTypes = map[string]string{
	"audio/wav":   "wav",
	"audio/x-wav": "wav",
	"audio/wave":  "wav",
	"audio/mpeg":  "mp3",
	"audio/mp3":   "mp3",
}

// MaxAudioUploadBytes caps a single consultation recording. 500 MiB covers
// several hours of WAV at telephone quality with headroom; anything larger
// is almost certainly a client error, not a real OPD consultation.
const MaxAudioUploadBytes = 500 * 1024 * 1024

var sha256HexRe = regexp.MustCompile(`^[0-9a-f]{64}$`)

// ValidSHA256Hex reports whether s looks like a lowercase-hex sha256 digest.
func ValidSHA256Hex(s string) bool {
	return sha256HexRe.MatchString(s)
}

// SourceAudioKey builds the content-addressed key for a consultation's raw
// audio (docs/architecture.md §3.3): immutable, shared across every ablation
// arm, and self-deduplicating since identical uploads hash to the same key.
func (c *Client) SourceAudioKey(consultationID uuid.UUID, sha256Hex, ext string) string {
	return fmt.Sprintf("%s/consultations/%s/source/audio/%s.%s", c.Env, consultationID, sha256Hex, ext)
}

// SourceAudioKeyPrefix is the prefix every valid SourceAudioKey for this
// consultation must start with — used to reject a confirm request whose
// object_key points outside this consultation's own namespace.
func (c *Client) SourceAudioKeyPrefix(consultationID uuid.UUID) string {
	return fmt.Sprintf("%s/consultations/%s/source/audio/", c.Env, consultationID)
}
