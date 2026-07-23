#include "report_renderer.hpp"

#include <cerrno>
#include <cctype>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

namespace fs = std::filesystem;

namespace {

constexpr const char* kName = "hls-quality-report-renderer";
constexpr const char* kVersion = "1.0.0";

struct Error : std::runtime_error {
    using std::runtime_error::runtime_error;
};

struct Options {
    fs::path report_json;
    fs::path dashboard_json;
    fs::path output;
    std::string fingerprint;
    std::string title;
    bool have_dashboard = false;
};

void print_usage(std::ostream& output) {
    output
        << "Usage: " << kName
        << " --report-json FILE --output FILE [options]\n\n"
        << "Options:\n"
        << "  --dashboard-json FILE  Compact scene and exact HLS-segment dashboard\n"
        << "  --fingerprint VALUE    Stable source fingerprint embedded in report metadata\n"
        << "  --title VALUE          Human-readable video title for dashboard-only reports\n"
        << "  --version              Show renderer version\n"
        << "  --help                 Show this help\n";
}

Options parse_arguments(int argc, char** argv) {
    Options options;
    bool have_report = false;
    bool have_output = false;
    for (int index = 1; index < argc; ++index) {
        const std::string name = argv[index];
        if (name == "--help" || name == "-h") {
            print_usage(std::cout);
            std::exit(0);
        }
        if (name == "--version") {
            std::cout << kName << ' ' << kVersion << '\n';
            std::exit(0);
        }
        if (index + 1 >= argc) throw Error("Missing value for " + name);
        const std::string value = argv[++index];
        if (name == "--report-json") {
            if (have_report) throw Error("--report-json may only be supplied once");
            options.report_json = value;
            have_report = true;
        } else if (name == "--dashboard-json") {
            if (options.have_dashboard) throw Error("--dashboard-json may only be supplied once");
            options.dashboard_json = value;
            options.have_dashboard = true;
        } else if (name == "--output") {
            if (have_output) throw Error("--output may only be supplied once");
            options.output = value;
            have_output = true;
        } else if (name == "--fingerprint") {
            options.fingerprint = value;
        } else if (name == "--title") {
            options.title = value;
        } else {
            throw Error("Unknown option " + name);
        }
    }
    if (!have_report || !have_output) {
        throw Error("--report-json and --output are required");
    }
    if (options.report_json.empty() || options.output.empty()) {
        throw Error("Input and output paths cannot be empty");
    }
    return options;
}

std::string read_file(const fs::path& path, const std::string& label) {
    std::error_code error;
    const fs::file_status status = fs::status(path, error);
    if (error || !fs::is_regular_file(status)) {
        throw Error(label + " is not a readable regular file: " + path.string());
    }
    std::ifstream input(path, std::ios::binary);
    if (!input) throw Error("Cannot open " + label + ": " + path.string());
    input.seekg(0, std::ios::end);
    const std::streamoff size = input.tellg();
    if (size <= 0) throw Error(label + " is empty: " + path.string());
    input.seekg(0, std::ios::beg);
    std::string contents(static_cast<std::size_t>(size), '\0');
    input.read(contents.data(), size);
    if (!input) throw Error("Cannot read " + label + ": " + path.string());
    return contents;
}

class JsonValidator {
public:
    explicit JsonValidator(std::string_view input) : input_(input) {}

    void validate_object(const std::string& label) {
        skip_space();
        if (peek() != '{') fail(label + " must contain a JSON object");
        parse_value(0);
        skip_space();
        if (position_ != input_.size()) fail(label + " has trailing data");
    }

private:
    std::string_view input_;
    std::size_t position_ = 0;

    [[noreturn]] void fail(const std::string& message) const {
        throw Error(
            message + " near byte " + std::to_string(position_));
    }

    char peek() const {
        return position_ < input_.size() ? input_[position_] : '\0';
    }

    void skip_space() {
        while (
            position_ < input_.size() &&
            (input_[position_] == ' ' || input_[position_] == '\t' ||
             input_[position_] == '\r' || input_[position_] == '\n')
        ) {
            ++position_;
        }
    }

    bool consume(char character) {
        if (peek() != character) return false;
        ++position_;
        return true;
    }

    void expect(std::string_view token) {
        if (input_.substr(position_, token.size()) != token) {
            fail("Invalid JSON value");
        }
        position_ += token.size();
    }

    void parse_value(unsigned depth) {
        if (depth > 1024) fail("JSON nesting is too deep");
        skip_space();
        switch (peek()) {
            case '{': parse_object(depth + 1); break;
            case '[': parse_array(depth + 1); break;
            case '"': parse_string(); break;
            case 't': expect("true"); break;
            case 'f': expect("false"); break;
            case 'n': expect("null"); break;
            default: parse_number();
        }
    }

    void parse_object(unsigned depth) {
        consume('{');
        skip_space();
        if (consume('}')) return;
        for (;;) {
            skip_space();
            if (peek() != '"') fail("JSON object key must be a string");
            parse_string();
            skip_space();
            if (!consume(':')) fail("JSON object key is missing ':'");
            parse_value(depth);
            skip_space();
            if (consume('}')) return;
            if (!consume(',')) fail("JSON object entries must be comma-separated");
        }
    }

    void parse_array(unsigned depth) {
        consume('[');
        skip_space();
        if (consume(']')) return;
        for (;;) {
            parse_value(depth);
            skip_space();
            if (consume(']')) return;
            if (!consume(',')) fail("JSON array entries must be comma-separated");
        }
    }

    void parse_string() {
        consume('"');
        while (position_ < input_.size()) {
            const unsigned char character =
                static_cast<unsigned char>(input_[position_++]);
            if (character == '"') return;
            if (character < 0x20) fail("JSON string contains a control character");
            if (character != '\\') continue;
            if (position_ >= input_.size()) fail("JSON string has an incomplete escape");
            const char escape = input_[position_++];
            if (
                escape == '"' || escape == '\\' || escape == '/' ||
                escape == 'b' || escape == 'f' || escape == 'n' ||
                escape == 'r' || escape == 't'
            ) {
                continue;
            }
            if (escape != 'u') fail("JSON string has an invalid escape");
            for (int digit = 0; digit < 4; ++digit) {
                if (
                    position_ >= input_.size() ||
                    !std::isxdigit(static_cast<unsigned char>(input_[position_]))
                ) {
                    fail("JSON string has an invalid Unicode escape");
                }
                ++position_;
            }
        }
        fail("JSON string is not terminated");
    }

    void parse_number() {
        const std::size_t start = position_;
        consume('-');
        if (consume('0')) {
            if (std::isdigit(static_cast<unsigned char>(peek()))) {
                fail("JSON number has a leading zero");
            }
        } else {
            if (!std::isdigit(static_cast<unsigned char>(peek()))) {
                fail("Invalid JSON value");
            }
            while (std::isdigit(static_cast<unsigned char>(peek()))) ++position_;
        }
        if (consume('.')) {
            if (!std::isdigit(static_cast<unsigned char>(peek()))) {
                fail("JSON number has an incomplete fraction");
            }
            while (std::isdigit(static_cast<unsigned char>(peek()))) ++position_;
        }
        if (peek() == 'e' || peek() == 'E') {
            ++position_;
            if (peek() == '+' || peek() == '-') ++position_;
            if (!std::isdigit(static_cast<unsigned char>(peek()))) {
                fail("JSON number has an incomplete exponent");
            }
            while (std::isdigit(static_cast<unsigned char>(peek()))) ++position_;
        }
        if (position_ == start) fail("Invalid JSON value");
    }
};

void atomic_write(const fs::path& destination, const std::string& contents) {
    const fs::path parent =
        destination.parent_path().empty() ? fs::path(".") : destination.parent_path();
    std::error_code error;
    if (!destination.parent_path().empty()) {
        fs::create_directories(parent, error);
        if (error) {
            throw Error(
                "Cannot create output directory " + parent.string() + ": " +
                error.message());
        }
    }

    std::string template_path =
        (parent / ("." + destination.filename().string() + ".XXXXXX")).string();
    std::vector<char> buffer(template_path.begin(), template_path.end());
    buffer.push_back('\0');
    int descriptor = ::mkstemp(buffer.data());
    if (descriptor < 0) {
        throw Error(
            "Cannot create temporary output for " + destination.string() + ": " +
            std::strerror(errno));
    }
    const fs::path temporary(buffer.data());

    const auto fail = [&](const std::string& operation) {
        const int saved_errno = errno;
        if (descriptor >= 0) {
            ::close(descriptor);
            descriptor = -1;
        }
        ::unlink(temporary.c_str());
        throw Error(
            operation + " " + destination.string() + ": " +
            std::strerror(saved_errno));
    };

    if (::fchmod(descriptor, S_IRUSR | S_IWUSR) != 0) {
        fail("Cannot secure temporary output for");
    }
    std::size_t offset = 0;
    while (offset < contents.size()) {
        const ssize_t written =
            ::write(descriptor, contents.data() + offset, contents.size() - offset);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) {
            if (written == 0) errno = EIO;
            fail("Cannot write");
        }
        offset += static_cast<std::size_t>(written);
    }
    while (::fsync(descriptor) != 0) {
        if (errno == EINTR) continue;
        fail("Cannot sync");
    }
    if (::close(descriptor) != 0) {
        const int saved_errno = errno;
        descriptor = -1;
        ::unlink(temporary.c_str());
        throw Error(
            "Cannot close temporary output " + temporary.string() + ": " +
            std::strerror(saved_errno));
    }
    descriptor = -1;
    if (::rename(temporary.c_str(), destination.c_str()) != 0) {
        const int saved_errno = errno;
        ::unlink(temporary.c_str());
        throw Error(
            "Cannot publish " + destination.string() + ": " +
            std::strerror(saved_errno));
    }
}

int run(const Options& options) {
    const std::string report = read_file(options.report_json, "report JSON");
    JsonValidator(report).validate_object("report JSON");

    std::string dashboard;
    if (options.have_dashboard) {
        dashboard = read_file(options.dashboard_json, "dashboard JSON");
        JsonValidator(dashboard).validate_object("dashboard JSON");
    }

    const std::string page =
        hls_quality_report::render(
            report, dashboard, options.fingerprint, options.title);
    atomic_write(options.output, page);
    std::cout
        << "Detailed quality report written to "
        << fs::absolute(options.output).string() << '\n';
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        return run(parse_arguments(argc, argv));
    } catch (const std::exception& error) {
        std::cerr << kName << ": " << error.what() << '\n';
        return 1;
    }
}
