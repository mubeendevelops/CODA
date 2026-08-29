package auth

import (
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/google/uuid"
)

func TestJWTService_IssueAndParse_RoundTrip(t *testing.T) {
	svc := NewJWTService("test-signing-key-at-least-16-bytes", time.Hour)
	userID, orgID := uuid.New(), uuid.New()

	token, err := svc.IssueAccessToken(userID, orgID, RoleDoctor)
	if err != nil {
		t.Fatalf("IssueAccessToken: %v", err)
	}

	claims, err := svc.ParseAccessToken(token)
	if err != nil {
		t.Fatalf("ParseAccessToken: %v", err)
	}
	if claims.UserID != userID {
		t.Errorf("UserID = %v, want %v", claims.UserID, userID)
	}
	if claims.OrgID != orgID {
		t.Errorf("OrgID = %v, want %v", claims.OrgID, orgID)
	}
	if claims.Role != RoleDoctor {
		t.Errorf("Role = %v, want %v", claims.Role, RoleDoctor)
	}
}

func TestJWTService_ParseAccessToken_Rejections(t *testing.T) {
	svc := NewJWTService("test-signing-key-at-least-16-bytes", time.Hour)
	otherSvc := NewJWTService("a-completely-different-signing-key", time.Hour)
	userID, orgID := uuid.New(), uuid.New()

	validToken, err := svc.IssueAccessToken(userID, orgID, RoleAdmin)
	if err != nil {
		t.Fatalf("IssueAccessToken: %v", err)
	}

	expiredSvc := NewJWTService("test-signing-key-at-least-16-bytes", -time.Hour)
	expiredToken, err := expiredSvc.IssueAccessToken(userID, orgID, RoleAdmin)
	if err != nil {
		t.Fatalf("IssueAccessToken (expired): %v", err)
	}

	noneAlgToken := forgeNoneAlgToken(t, userID, orgID)

	tests := []struct {
		name    string
		token   string
		wantErr error // nil means "any error"
	}{
		{"garbage string", "not-a-jwt-at-all", nil},
		{"empty string", "", nil},
		{"wrong signing key", mustSignWith(t, otherSvc, userID, orgID), ErrInvalidToken},
		{"expired token", expiredToken, ErrExpiredToken},
		{"tampered payload", tamperPayload(validToken), nil},
		{"alg=none forged token", noneAlgToken, nil},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := svc.ParseAccessToken(tt.token)
			if err == nil {
				t.Fatal("expected an error, got nil")
			}
		})
	}
}

func mustSignWith(t *testing.T, svc *JWTService, userID, orgID uuid.UUID) string {
	t.Helper()
	tok, err := svc.IssueAccessToken(userID, orgID, RoleDoctor)
	if err != nil {
		t.Fatalf("IssueAccessToken: %v", err)
	}
	return tok
}

func tamperPayload(token string) string {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return token + "x"
	}
	// Flip the last character of the payload segment so the signature no
	// longer matches, without touching JSON structure enough to fail
	// base64 decoding outright.
	payload := []byte(parts[1])
	last := payload[len(payload)-1]
	if last == 'A' {
		payload[len(payload)-1] = 'B'
	} else {
		payload[len(payload)-1] = 'A'
	}
	parts[1] = string(payload)
	return strings.Join(parts, ".")
}

// forgeNoneAlgToken builds a token with alg=none and no signature, the
// classic JWT bypass — ParseAccessToken must reject it via the pinned
// HMAC-method check, not merely because the signature is absent.
func forgeNoneAlgToken(t *testing.T, userID, orgID uuid.UUID) string {
	t.Helper()
	claims := Claims{
		UserID: userID,
		OrgID:  orgID,
		Role:   RoleAdmin,
		RegisteredClaims: jwt.RegisteredClaims{
			ExpiresAt: jwt.NewNumericDate(time.Now().Add(time.Hour)),
		},
	}
	token := jwt.NewWithClaims(jwt.SigningMethodNone, claims)
	signed, err := token.SignedString(jwt.UnsafeAllowNoneSignatureType)
	if err != nil {
		t.Fatalf("forge none-alg token: %v", err)
	}
	return signed
}
