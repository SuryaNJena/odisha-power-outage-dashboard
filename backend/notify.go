package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"

	"github.com/segmentio/kafka-go"
)

// Producer publishes user outage reports to Kafka for the Flink/Spark NRT
// pipeline. It is best-effort: the report is also persisted in Postgres.
type Producer struct {
	writer *kafka.Writer
	topic  string
}

func NewProducer(brokers, topic string) *Producer {
	if brokers == "" || topic == "" {
		return nil
	}
	return &Producer{
		topic: topic,
		writer: &kafka.Writer{
			Addr:         kafka.TCP(splitCSV(brokers)...),
			Topic:        topic,
			Balancer:     &kafka.LeastBytes{},
			RequiredAcks: kafka.RequireOne,
			BatchTimeout: 50 * time.Millisecond,
			WriteTimeout: 6 * time.Second,
			Async:        false,
		},
	}
}

func (p *Producer) Close() {
	if p != nil && p.writer != nil {
		_ = p.writer.Close()
	}
}

func (p *Producer) PublishReport(ctx context.Context, r ReportInput, id string) error {
	if p == nil {
		return fmt.Errorf("kafka producer not configured")
	}
	event := map[string]any{
		"report_id":   id,
		"district":    r.District,
		"gss":         r.GSS,
		"feeder":      r.Feeder,
		"comment":     r.Comment,
		"severity":    r.Severity,
		"reporter":    "web",
		"lat":         r.Lat,
		"lon":         r.Lon,
		"reported_at": time.Now().UTC().Format(time.RFC3339),
	}
	body, err := json.Marshal(event)
	if err != nil {
		return err
	}
	// Bounded write budget: the API must stay responsive when Kafka is down.
	wctx, cancel := context.WithTimeout(ctx, 6*time.Second)
	defer cancel()
	return p.writer.WriteMessages(wctx, kafka.Message{
		Key:   []byte(r.District),
		Value: body,
		Time:  time.Now(),
	})
}

// newReportID returns a random UUIDv4 string.
func newReportID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return fmt.Sprintf("fallback-%d", time.Now().UnixNano())
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	dst := make([]byte, 36)
	hex.Encode(dst[0:8], b[0:4])
	dst[8] = '-'
	hex.Encode(dst[9:13], b[4:6])
	dst[13] = '-'
	hex.Encode(dst[14:18], b[6:8])
	dst[18] = '-'
	hex.Encode(dst[19:23], b[8:10])
	dst[23] = '-'
	hex.Encode(dst[24:36], b[10:16])
	return string(dst)
}

func splitCSV(s string) []string {
	out := []string{}
	cur := ""
	for _, r := range s {
		if r == ',' {
			if cur != "" {
				out = append(out, cur)
				cur = ""
			}
			continue
		}
		cur += string(r)
	}
	if cur != "" {
		out = append(out, cur)
	}
	if len(out) == 0 {
		out = []string{s}
	}
	return out
}
