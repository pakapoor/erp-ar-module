#!/bin/bash

TOKEN="eyJhbGciOiJIUzI1NiIsImtpZCI6ImRldi1rZXktMDAxIiwidHlwIjoiSldUIn0.eyJzdWIiOiIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDMiLCJ1c2VyX2lkIjoiMDAwMDAwMDAtMDAwMC0wMDAwLTAwMDAtMDAwMDAwMDAwMDAzIiwidGVuYW50X2lkIjoiMDAwMDAwMDAtMDAwMC0wMDAwLTAwMDAtMDAwMDAwMDAwMDAxIiwiZW50aXR5X2lkIjoiMDAwMDAwMDAtMDAwMC0wMDAwLTAwMDAtMDAwMDAwMDAwMDAyIiwicm9sZXMiOlsiaW52b2ljZV9jcmVhdG9yIl0sImV4cCI6MTc4NDQ3NTQzNSwiaWF0IjoxNzg0NDcxODM1fQ.t3Z5UD8e8CSEknpWKfM4ZH0yONu9qJLZ7RsD1_6cIeU"
PRIYA_TOKEN="eyJhbGciOiJIUzI1NiIsImtpZCI6ImRldi1rZXktMDAxIiwidHlwIjoiSldUIn0.eyJzdWIiOiIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDQiLCJ1c2VyX2lkIjoiMDAwMDAwMDAtMDAwMC0wMDAwLTAwMDAtMDAwMDAwMDAwMDA0IiwidGVuYW50X2lkIjoiMDAwMDAwMDAtMDAwMC0wMDAwLTAwMDAtMDAwMDAwMDAwMDAxIiwiZW50aXR5X2lkIjoiMDAwMDAwMDAtMDAwMC0wMDAwLTAwMDAtMDAwMDAwMDAwMDAyIiwicm9sZXMiOlsiaW52b2ljZV9hcHByb3ZlciIsImNmbyJdLCJleHAiOjE3ODQ0NzU2ODAsImlhdCI6MTc4NDQ3MjA4MH0.iOZpEroXW0uFU6bBlc8ogvQ-bUnwqacDRXa4w9YR4WA"

echo "=== API1: POST /invoices ==="
curl -s -X POST http://localhost:8000/api/v1/invoices \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000" \
  -d '{
    "customer_id": "00000000-0000-0000-0000-000000000005",
    "po_reference": "PO-2024-789",
    "invoice_date": "2026-07-19",
    "payment_terms": "NET30",
    "currency": "INR",
    "line_items": [
      {
        "description": "Industrial Pump",
        "quantity": 2,
        "unit_price": 50000,
        "tax_rate": 18,
        "tax_jurisdiction": "MH"
      },
      {
        "description": "Safety Valves",
        "quantity": 10,
        "unit_price": 5000,
        "tax_rate": 12,
        "tax_jurisdiction": "KA"
      }
    ]
  }' | python3 -m json.tool

echo "=== API2: GET /invoices/{id} ==="
curl -s http://localhost:8000/api/v1/invoices/4987680d-830b-4305-93c9-c01b5b66c4e4 \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

echo "=== API3: POST /invoices/{id}/approve ==="
curl -s -X POST http://localhost:8000/api/v1/invoices/4987680d-830b-4305-93c9-c01b5b66c4e4/approve \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $PRIYA_TOKEN" \
  -H "X-Idempotency-Key: 660e8400-e29b-41d4-a716-446655440001" \
  -H "If-Match: 1" \
  -d '{"notes": "Approved after tax jurisdiction check"}' | python3 -m json.tool
