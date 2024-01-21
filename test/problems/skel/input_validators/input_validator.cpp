#include <iostream>

int main(int argc, char *argv[]) {
    int n;
    std::cin >> n;
    return 0 <= n && n < 1000000 ? 42 : 43;
}
