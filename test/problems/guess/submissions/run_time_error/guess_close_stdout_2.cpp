#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <thread>
int main() {
	using namespace std::chrono_literals;
	std::fclose(stdout);
	std::this_thread::sleep_for(0.5s);
	exit(1);
}
