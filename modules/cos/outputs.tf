output "bucket_name" {
  description = "Full bucket name (prefix-account_id)"
  value       = tencentcloud_cos_bucket.this.bucket
}
