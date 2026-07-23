CXX ?= c++
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wextra -Wpedantic
CPPFLAGS ?=
LDFLAGS ?=
LDLIBS ?= -pthread

TARGET := hls-quality-analyzer
SOURCE := src/main.cpp

ifeq ($(shell uname -s),Darwin)
MACOS_SDK := $(shell xcrun --show-sdk-path 2>/dev/null)
ifneq ($(strip $(MACOS_SDK)),)
CPPFLAGS += -isysroot $(MACOS_SDK)
LDFLAGS += -isysroot $(MACOS_SDK)
endif
endif

.PHONY: all clean test

all: $(TARGET)

$(TARGET): $(SOURCE)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(SOURCE) -o $@ $(LDFLAGS) $(LDLIBS)

test: $(TARGET)
	./test-smoke.sh

clean:
	rm -f -- $(TARGET)
