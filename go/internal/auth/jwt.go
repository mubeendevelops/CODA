package auth

import (
	"errors"
	"fmt"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/google/uuid"
)

var (
	ErrExpiredToken = errors.New("auth: token expired")
	ErrInvalidToken = errors.New("auth: invalid token")
)

// Claims is the access-token payload. It carries just enough to authorize a
// request without a database round trip: org scoping and role checks both
// read straight off the token (docs/architecture.md §7.6 — "org-scoped row
// filtering on every query").
type Claims struct {
	UserID uuid.UUID `json:"uid"`
	OrgID  uuid.UUID `json:"org"`
	Role   Role      `json:"role"`
	jwt.RegisteredClaims
}

// JWTService issues and verifies access tokens. It holds no state beyond
// its configuration, so one instance is shared across all requests.
type JWTService struct {
	signingKey []byte
	ttl        time.Duration
	issuer     string
}

func NewJWTService(signingKey string, ttl time.Duration) *JWTService {
	return &JWTService{signingKey: []byte(signingKey), ttl: ttl, issuer: "coda-go-api"}
}

// IssueAccessToken signs a short-lived JWT for the given user identity.
func (s *JWTService) IssueAccessToken(userID, orgID uuid.UUID, role Role) (string, error) {
	now := time.Now().UTC()
	claims := Claims{
		UserID: userID,
		OrgID:  orgID,
		Role:   role,
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer:    s.issuer,
			Subject:   userID.String(),
			IssuedAt:  jwt.NewNumericDate(now),
			NotBefore: jwt.NewNumericDate(now),
			ExpiresAt: jwt.NewNumericDate(now.Add(s.ttl)),
			ID:        uuid.NewString(),
		},
	}
	token := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
	signed, err := token.SignedString(s.signingKey)
	if err != nil {
		return "", fmt.Errorf("auth: sign token: %w", err)
	}
	return signed, nil
}

// ParseAccessToken verifies signature, expiry, and issuer, returning the
// embedded claims. It explicitly pins the expected signing method — never
// trust `alg` from the token itself — so a token forged with `alg: none`
// or an attacker-chosen algorithm is rejected before signature checking.
func (s *JWTService) ParseAccessToken(tokenString string) (*Claims, error) {
	claims := &Claims{}
	token, err := jwt.ParseWithClaims(tokenString, claims, func(t *jwt.Token) (interface{}, error) {
		if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
			return nil, fmt.Errorf("auth: unexpected signing method %v", t.Header["alg"])
		}
		return s.signingKey, nil
	}, jwt.WithIssuer(s.issuer), jwt.WithExpirationRequired())

	if err != nil {
		if errors.Is(err, jwt.ErrTokenExpired) {
			return nil, ErrExpiredToken
		}
		return nil, fmt.Errorf("%w: %v", ErrInvalidToken, err)
	}
	if !token.Valid {
		return nil, ErrInvalidToken
	}
	return claims, nil
}
