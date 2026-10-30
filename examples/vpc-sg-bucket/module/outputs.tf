output "vpc_id" {
  value = aws_vpc.main.id
}

output "app_security_group_id" {
  value = aws_security_group.app.id
}

output "log_bucket" {
  value = aws_s3_bucket.logs.bucket
}
