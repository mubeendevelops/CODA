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
// needs: presigned uploads and downloads, existence/size checks, and
// deletion — never streaming object bytes through go-api itself
// (docs/architecture.md §1.2: go-api must not run ML or hold pipeline data
// in memory; objects move directly between the browser and MinIO via
// presigned URLs in both directions, upload and download alike).
type Client struct {
	mc       *minio.Client // Docker-network endpoint — go-api's own direct calls
	presigns *minio.Client // browser-reachable endpoint — presigned URL generation only
	Bucket   string
	Env      string // artifact key prefix (architecture.md §3.3), e.g. "dev"
}

// NewClient constructs a Client and verifies the target bucket exists,
// creating it if not — the same "fail fast at startup" posture as db.NewPool.
//
// Two minio.Client instances, same credentials, different endpoints
// (config.Storage's doc comment explains why): `mc` talks to
// cfg.Endpoint for go-api's own direct calls (StatObject, bucket checks —
// these need a real, live connection, so they must use the address
// actually reachable from inside the compose network); `presigns` is
// constructed against cfg.PublicEndpoint purely to compute presigned-URL
// signatures. Computing a signature itself needs no live connection to
// that address — but the SDK's Presigned{Put,Get}Object still calls
// GetBucketLocation first to learn the bucket's region for the signature,
// and that call WOULD go out over `presigns`' own (unreachable) endpoint
// if left to look it up itself. So `presigns` is given an explicit Region,
// learned via one real GetBucketLocation call over the connection that
// actually works (`mc`) — discovered empirically: without this, every
// presigned URL failed with a connection-refused dialing PublicEndpoint
// from inside the container, since bucket-region lookup isn't a pure
// offline computation the way the final signature step is.
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

	presigns := mc
	if cfg.PublicEndpoint != cfg.Endpoint {
		region, err := mc.GetBucketLocation(ctx, cfg.Bucket)
		if err != nil {
			return nil, fmt.Errorf("storage: get bucket region for presigning: %w", err)
		}
		presigns, err = minio.New(cfg.PublicEndpoint, &minio.Options{
			Creds:  credentials.NewStaticV4(cfg.AccessKey, cfg.SecretKey, ""),
			Secure: cfg.UseSSL,
			Region: region,
		})
		if err != nil {
			return nil, fmt.Errorf("storage: construct presigning minio client: %w", err)
		}
	}

	return &Client{mc: mc, presigns: presigns, Bucket: cfg.Bucket, Env: env}, nil
}

// PresignPutObject returns a time-limited URL the caller can PUT directly
// to, so audio bytes never transit go-api (docs/architecture.md §1.2). The
// content type is not baked into the presigned URL by this SDK — go-api
// validates it up front (handler layer) and StatObject re-checks it on
// confirm, since a presigned PUT URL cannot itself enforce the header a
// client sends.
func (c *Client) PresignPutObject(ctx context.Context, key string, expiry time.Duration) (*url.URL, error) {
	u, err := c.presigns.PresignedPutObject(ctx, c.Bucket, key, expiry)
	if err != nil {
		return nil, fmt.Errorf("storage: presign put %q: %w", key, err)
	}
	return u, nil
}

// PresignGetObject returns a time-limited URL the caller can GET directly
// from MinIO — the download-side mirror of PresignPutObject, for the same
// reason: go-api must not stream artifact bytes through itself
// (docs/architecture.md §1.2). Used to hand the frontend a transcript
// artifact without go-api ever reading its content server-side.
func (c *Client) PresignGetObject(ctx context.Context, key string, expiry time.Duration) (*url.URL, error) {
	u, err := c.presigns.PresignedGetObject(ctx, c.Bucket, key, expiry, url.Values{})
	if err != nil {
		return nil, fmt.Errorf("storage: presign get %q: %w", key, err)
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
