CXX ?= c++
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wextra -Wpedantic
CPPFLAGS ?=
LDFLAGS ?=
LDLIBS ?= -pthread

ANALYZER_TARGET := hls-quality-analyzer
ANALYZER_SOURCE := src/main.cpp
RENDERER_TARGET := hls-quality-report-renderer
RENDERER_SOURCE := src/report_renderer_main.cpp
TARGETS := $(ANALYZER_TARGET) $(RENDERER_TARGET)

ifeq ($(shell uname -s),Darwin)
MACOS_SDK := $(shell xcrun --show-sdk-path 2>/dev/null)
ifneq ($(strip $(MACOS_SDK)),)
CPPFLAGS += -isysroot $(MACOS_SDK)
LDFLAGS += -isysroot $(MACOS_SDK)
endif
endif

.PHONY: all clean test

all: $(TARGETS)

$(ANALYZER_TARGET): $(ANALYZER_SOURCE) src/report_renderer.hpp
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(ANALYZER_SOURCE) -o $@ $(LDFLAGS) $(LDLIBS)

$(RENDERER_TARGET): $(RENDERER_SOURCE) src/report_renderer.hpp
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(RENDERER_SOURCE) -o $@ $(LDFLAGS)

test: $(TARGETS)
	./test-renderer.sh
	./test-smoke.sh

clean:
	rm -f -- $(TARGETS)
