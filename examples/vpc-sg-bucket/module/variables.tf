variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "eu-west-1"
}

variable "name" {
  description = "Prefix for resource names."
  type        = string
  default     = "demo"
}
