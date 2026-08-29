package storage

import (
	"context"
	"fmt"
	"net/url"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"

	"coda/go/internal/config"
)

// Client wraps the MinIO SDK behind the narrow set of operations go-api
// needs: presigned uploads, existence/size checks, and deletion — never
// streaming object bytes through go-api itself (docs/architecture.md §1.2:
// go-api must not run ML or hold pipeline data in memory; large objects
// move directly between the browser and MinIO via presigned URLs).
type Client struct {
	mc     *minio.Client
	Bucket string
	Env    string // artifact key prefix (architecture.md §3.3), e.g. "dev"
}

// NewClient constructs a Client and verifies the target bucket exists,
// creating it if not — the same "fail fast at startup" posture as db.NewPool.
func NewClient(ctx context.Context, cfg config.Storage, env string) (*Client, error) {
	mc, err := minio.New(cfg.Endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(cfg.AccessKey, cfg.SecretKey, ""),
		Secure: cfg.UseSSL,
	})
	if err != nil {
		return nil, fmt.Errorf("storage: construct minio client: %w", err)
	}

	exists, err := mc.BucketExists(ctx, cfg.Bucket)
	if err != nil {
		return nil, fmt.Errorf("storage: check bucket %q: %w", cfg.Bucket, err)
	}
	if !exists {
		if err := mc.MakeBucket(ctx, cfg.Bucket, minio.MakeBucketOptions{}); err != nil {
			return nil, fmt.Errorf("storage: create bucket %q: %w", cfg.Bucket, err)
		}
	}

	return &Client{mc: mc, Bucket: cfg.Bucket, Env: env}, nil
}

// PresignPutObject returns a time-limited URL the caller can PUT directly
// to, so audio bytes never transit go-api (docs/architecture.md §1.2). The
// content type is not baked into the presigned URL by this SDK — go-api
// validates it up front (handler layer) and StatObject re-checks it on
// confirm, since a presigned PUT URL cannot itself enforce the header a
// client sends.
func (c *Client) PresignPutObject(ctx context.Context, key string, expiry time.Duration) (*url.URL, error) {
	u, err := c.mc.PresignedPutObject(ctx, c.Bucket, key, expiry)
	if err != nil {
		return nil, fmt.Errorf("storage: presign put %q: %w", key, err)
	}
	return u, nil
}

// StatObject verifies an object exists and returns its metadata — used by
// the audio-confirm endpoint to check the upload actually landed before
// trusting the client's claimed checksum/duration.
func (c *Client) StatObject(ctx context.Context, key string) (minio.ObjectInfo, error) {
	return c.mc.StatObject(ctx, c.Bucket, key, minio.StatObjectOptions{})
}

// IsNotFound reports whether err is MinIO's "no such key" — the confirm
// handler needs to distinguish "not uploaded yet" (a normal, retryable
// client state) from a genuine storage-layer failure.
func IsNotFound(err error) bool {
	resp := minio.ToErrorResponse(err)
	return resp.Code == "NoSuchKey"
}

// RemoveObject deletes one object. Used by DPDP erasure/deletion — ignores
// "already gone" so erasure is idempotent under retry.
func (c *Client) RemoveObject(ctx context.Context, key string) error {
	err := c.mc.RemoveObject(ctx, c.Bucket, key, minio.RemoveObjectOptions{})
	if err != nil && !IsNotFound(err) {
		return fmt.Errorf("storage: remove object %q: %w", key, err)
	}
	return nil
}
