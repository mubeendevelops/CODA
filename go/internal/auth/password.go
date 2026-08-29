package auth

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"

	"golang.org/x/crypto/argon2"
)

// Argon2id parameters per OWASP's current minimum recommendation for
// interactive login (19 MiB memory floor rounded up to a tidy 64 MiB, one
// iteration, one thread per lane doubled for parallelism headroom). Encoded
// into every hash (see encode/decode below) rather than assumed at verify
// time, so a future parameter change doesn't break verification of
// passwords hashed under the old parameters.
const (
	argonTime    = 1
	argonMemory  = 64 * 1024 // KiB
	argonThreads = 4
	argonKeyLen  = 32
	argonSaltLen = 16
)

var ErrInvalidHash = errors.New("auth: invalid password hash format")
var ErrIncompatibleVersion = errors.New("auth: incompatible argon2 version")

// HashPassword returns a self-describing Argon2id hash string
// ($argon2id$v=19$m=...,t=...,p=...$salt$hash, base64 raw-std encoding for
// salt/hash) so parameters can change over time without invalidating
// already-issued hashes.
func HashPassword(plain string) (string, error) {
	salt := make([]byte, argonSaltLen)
	if _, err := rand.Read(salt); err != nil {
		return "", fmt.Errorf("auth: generate salt: %w", err)
	}
	hash := argon2.IDKey([]byte(plain), salt, argonTime, argonMemory, argonThreads, argonKeyLen)
	return encodeHash(salt, hash), nil
}

// VerifyPassword reports whether plain matches encodedHash, using a
// constant-time comparison on the derived key so timing cannot leak how
// many bytes matched.
func VerifyPassword(encodedHash, plain string) (bool, error) {
	params, salt, hash, err := decodeHash(encodedHash)
	if err != nil {
		return false, err
	}
	candidate := argon2.IDKey([]byte(plain), salt, params.time, params.memory, params.threads, uint32(len(hash)))
	return subtle.ConstantTimeCompare(candidate, hash) == 1, nil
}

type argonParams struct {
	time    uint32
	memory  uint32
	threads uint8
}

func encodeHash(salt, hash []byte) string {
	return fmt.Sprintf("$argon2id$v=%d$m=%d,t=%d,p=%d$%s$%s",
		argon2.Version, argonMemory, argonTime, argonThreads,
		b64Encode(salt), b64Encode(hash))
}

func decodeHash(encoded string) (argonParams, []byte, []byte, error) {
	parts := strings.Split(encoded, "$")
	if len(parts) != 6 || parts[1] != "argon2id" {
		return argonParams{}, nil, nil, ErrInvalidHash
	}

	var version int
	if _, err := fmt.Sscanf(parts[2], "v=%d", &version); err != nil {
		return argonParams{}, nil, nil, ErrInvalidHash
	}
	if version != argon2.Version {
		return argonParams{}, nil, nil, ErrIncompatibleVersion
	}

	var p argonParams
	var threads int
	if _, err := fmt.Sscanf(parts[3], "m=%d,t=%d,p=%d", &p.memory, &p.time, &threads); err != nil {
		return argonParams{}, nil, nil, ErrInvalidHash
	}
	p.threads = uint8(threads)

	salt, err := b64Decode(parts[4])
	if err != nil {
		return argonParams{}, nil, nil, fmt.Errorf("%w: salt: %v", ErrInvalidHash, err)
	}
	hash, err := b64Decode(parts[5])
	if err != nil {
		return argonParams{}, nil, nil, fmt.Errorf("%w: hash: %v", ErrInvalidHash, err)
	}
	return p, salt, hash, nil
}

func b64Encode(b []byte) string { return base64.RawStdEncoding.EncodeToString(b) }

func b64Decode(s string) ([]byte, error) { return base64.RawStdEncoding.DecodeString(s) }
