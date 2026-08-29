package config

import (
	"testing"
	"time"
)

func TestAuth_Validate(t *testing.T) {
	tests := []struct {
		name    string
		cfg     Auth
		wantErr bool
	}{
		{
			name:    "valid config",
			cfg:     Auth{JWTSigningKey: "0123456789abcdef", AccessTokenTTL: 15 * time.Minute, RefreshTokenTTL: 7 * 24 * time.Hour},
			wantErr: false,
		},
		{
			name:    "signing key too short",
			cfg:     Auth{JWTSigningKey: "short", AccessTokenTTL: 15 * time.Minute, RefreshTokenTTL: 7 * 24 * time.Hour},
			wantErr: true,
		},
		{
			name:    "empty signing key",
			cfg:     Auth{JWTSigningKey: "", AccessTokenTTL: 15 * time.Minute, RefreshTokenTTL: 7 * 24 * time.Hour},
			wantErr: true,
		},
		{
			name:    "zero access token TTL",
			cfg:     Auth{JWTSigningKey: "0123456789abcdef", AccessTokenTTL: 0, RefreshTokenTTL: 7 * 24 * time.Hour},
			wantErr: true,
		},
		{
			name:    "negative access token TTL",
			cfg:     Auth{JWTSigningKey: "0123456789abcdef", AccessTokenTTL: -time.Minute, RefreshTokenTTL: 7 * 24 * time.Hour},
			wantErr: true,
		},
		{
			name:    "refresh TTL shorter than access TTL",
			cfg:     Auth{JWTSigningKey: "0123456789abcdef", AccessTokenTTL: time.Hour, RefreshTokenTTL: time.Minute},
			wantErr: true,
		},
		{
			name:    "refresh TTL equal to access TTL",
			cfg:     Auth{JWTSigningKey: "0123456789abcdef", AccessTokenTTL: time.Hour, RefreshTokenTTL: time.Hour},
			wantErr: true,
		},
		{
			name:    "exactly minimum signing key length",
			cfg:     Auth{JWTSigningKey: "1234567890123456", AccessTokenTTL: 15 * time.Minute, RefreshTokenTTL: 7 * 24 * time.Hour},
			wantErr: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := tt.cfg.Validate()
			if (err != nil) != tt.wantErr {
				t.Errorf("Validate() error = %v, wantErr %v", err, tt.wantErr)
			}
		})
	}
}

func TestAuth_LogValue_NeverLeaksSigningKey(t *testing.T) {
	cfg := Auth{JWTSigningKey: "a-very-secret-key-nobody-should-see", AccessTokenTTL: time.Minute, RefreshTokenTTL: time.Hour}

	if got := cfg.String(); containsSubstring(got, cfg.JWTSigningKey) {
		t.Errorf("String() leaked the signing key: %s", got)
	}

	logged := cfg.LogValue().String()
	if containsSubstring(logged, cfg.JWTSigningKey) {
		t.Errorf("LogValue() leaked the signing key: %s", logged)
	}
}

func containsSubstring(haystack, needle string) bool {
	if needle == "" {
		return false
	}
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}
