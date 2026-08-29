package http

import (
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
	requestsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "coda_http_requests_total",
		Help: "Total HTTP requests handled by go-api, by method, route pattern, and status code.",
	}, []string{"method", "route", "status"})

	requestDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "coda_http_request_duration_seconds",
		Help:    "go-api HTTP request latency in seconds, by method and route pattern.",
		Buckets: prometheus.DefBuckets,
	}, []string{"method", "route"})
)

// MetricsMiddleware records per-request count and latency series
// (docs/architecture.md §8: "stage duration histograms" at the HTTP layer).
// It must run after chi's router has matched the route so RoutePattern()
// is populated — mounted last in the middleware stack, just before routes.
func MetricsMiddleware() func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			ww := middleware.NewWrapResponseWriter(w, r.ProtoMajor)
			next.ServeHTTP(ww, r)

			route := chi.RouteContext(r.Context()).RoutePattern()
			if route == "" {
				route = "unmatched"
			}
			status := ww.Status()
			if status == 0 {
				status = http.StatusOK
			}

			requestsTotal.WithLabelValues(r.Method, route, strconv.Itoa(status)).Inc()
			requestDuration.WithLabelValues(r.Method, route).Observe(time.Since(start).Seconds())
		})
	}
}

// NewMetricsHandler serves the Prometheus exposition format. Mounted on a
// separate port (docs/architecture.md §1.1: go-api exposes 9090 for
// metrics, distinct from 8080 for REST) so metrics scraping is never
// gated behind the same rate limiter, CORS, or auth as the API surface.
func NewMetricsHandler() http.Handler {
	return promhttp.Handler()
}
