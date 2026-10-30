# --- Network -----------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true

  tags = {
    Name = var.name
  }
}

resource "aws_subnet" "app" {
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.0.1.0/24"

  tags = {
    Name = "${var.name}-app"
  }
}

resource "aws_subnet" "alb" {
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.0.10.0/24"

  tags = {
    Name = "${var.name}-alb"
  }
}

resource "aws_subnet" "bastion" {
  vpc_id     = aws_vpc.main.id
  cidr_block = "10.0.100.0/28"

  tags = {
    Name = "${var.name}-bastion"
  }
}

# --- App security group --------------------------------------------------------

resource "aws_security_group" "app" {
  name        = "${var.name}-app"
  description = "App servers"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "app_http" {
  security_group_id = aws_security_group.app.id
  description       = "HTTP from the load balancer subnet"
  ip_protocol       = "tcp"
  from_port         = 8080
  to_port           = 8080
  cidr_ipv4         = aws_subnet.alb.cidr_block
}

resource "aws_vpc_security_group_ingress_rule" "ssh_bastion" {
  security_group_id = aws_security_group.app.id
  description       = "SSH from the bastion subnet only"
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
  cidr_ipv4         = aws_subnet.bastion.cidr_block
}

# --- Log bucket ----------------------------------------------------------------

resource "aws_s3_bucket" "logs" {
  bucket = "${var.name}-logs"
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket = aws_s3_bucket.logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id

  versioning_configuration {
    status = "Enabled"
  }
}
