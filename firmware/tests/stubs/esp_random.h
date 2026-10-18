#pragma once
#include <stdint.h>
inline uint32_t hostRandom = 0;
inline uint32_t esp_random() { return hostRandom++; }
