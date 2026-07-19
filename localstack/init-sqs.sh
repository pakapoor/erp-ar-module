#!/bin/bash

set -euo pipefail

AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
DELIVERY_QUEUE_NAME="${DELIVERY_QUEUE_NAME:-invoice-delivery}"
DELIVERY_DLQ_NAME="${DELIVERY_DLQ_NAME:-invoice-delivery-dlq}"

awslocal sqs create-queue \
  --queue-name "$DELIVERY_DLQ_NAME" \
  --attributes MessageRetentionPeriod=1209600 >/dev/null

dlq_url="$(awslocal sqs get-queue-url \
  --queue-name "$DELIVERY_DLQ_NAME" \
  --query QueueUrl \
  --output text)"
dlq_arn="$(awslocal sqs get-queue-attributes \
  --queue-url "$dlq_url" \
  --attribute-names QueueArn \
  --query Attributes.QueueArn \
  --output text)"
redrive_policy="{\"deadLetterTargetArn\":\"$dlq_arn\",\"maxReceiveCount\":\"3\"}"

awslocal sqs create-queue \
  --queue-name "$DELIVERY_QUEUE_NAME" \
  --attributes \
    "RedrivePolicy=$redrive_policy,VisibilityTimeout=3,ReceiveMessageWaitTimeSeconds=2" \
  >/dev/null

echo "Created SQS queues: $DELIVERY_QUEUE_NAME -> $DELIVERY_DLQ_NAME"
