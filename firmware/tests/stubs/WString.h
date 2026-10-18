#pragma once
#include <string>
class String {
 public:
  String() = default;
  String(const char* text) : value_(text) {}
  String(std::string text) : value_(std::move(text)) {}
  const char* c_str() const { return value_.c_str(); }
  size_t length() const { return value_.size(); }
  int compareTo(const String& other) const { return value_.compare(other.value_); }
  bool operator==(const String& other) const { return value_ == other.value_; }
  bool operator!=(const String& other) const { return !(*this == other); }
  String operator+(const String& other) const { return value_ + other.value_; }
 private:
  std::string value_;
};
