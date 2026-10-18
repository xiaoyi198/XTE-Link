#pragma once
// Explicit diagnostic build only. Creates/removes its own exclusive test file.
namespace xte {
inline const char* tfBenchmark(JsonDocument& response) {
  constexpr char path[] = "/xte-link/usb-test-20261018/sd-bench.xte-part";
  constexpr uint32_t size = 2 * 1024 * 1024 + 13;
  FsFile file = SdMan.open(path, O_RDWR | O_CREAT | O_EXCL);
  if (!file) return "benchmark_file_exists_or_open_failed";
  uint8_t block[1024];
  for (size_t i = 0; i < sizeof(block); ++i) block[i] = i & 255;
  const uint32_t started = micros();
  bool good = true;
  for (uint32_t offset = 0; offset < size && good;) {
    const size_t count = std::min(static_cast<uint32_t>(sizeof(block)), size - offset);
    good = file.write(block, count) == count;
    offset += count;
  }
  good = file.sync() && good;
  response["write_us"] = micros() - started;
  file.close();
  file = SdMan.open(path);
  if (!file || file.fileSize() != size) good = false;
  mbedtls_sha256_context hash;
  mbedtls_sha256_init(&hash);
  mbedtls_sha256_starts_ret(&hash, 0);
  const uint32_t readStarted = micros();
  uint32_t readUs = 0;
  for (uint32_t offset = 0; offset < size && good;) {
    const size_t count = std::min(static_cast<uint32_t>(sizeof(block)), size - offset);
    const uint32_t before = micros();
    good = file.read(block, count) == static_cast<int>(count);
    readUs += micros() - before;
    mbedtls_sha256_update_ret(&hash, block, count);
    offset += count;
  }
  response["read_hash_us"] = micros() - readStarted;
  response["read_io_us"] = readUs;
  uint8_t digest[32];
  mbedtls_sha256_finish_ret(&hash, digest);
  mbedtls_sha256_free(&hash);
  char hex[65];
  constexpr char digits[] = "0123456789abcdef";
  for (size_t i = 0; i < 32; ++i) { hex[i * 2] = digits[digest[i] >> 4]; hex[i * 2 + 1] = digits[digest[i] & 15]; }
  hex[64] = 0;
  response["sha256"] = hex;
  response["size"] = size;
  response["heap"] = ESP.getFreeHeap();
  response["stack_min_free"] = uxTaskGetStackHighWaterMark(nullptr);
  file.close();
  const bool removed = SdMan.remove(path);
  return good && removed ? nullptr : "benchmark_io_failed";
}
}
