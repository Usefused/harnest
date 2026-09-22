// pack-interpreter is a native test double used when PATH contains no Python.
package main

import (
	"bufio"
	"fmt"
	"os"
)

// main exercises stdin, configured environment and exit-code propagation.
func main() {
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Scan()
	fmt.Printf("%s:%s:%s\n", scanner.Text(), os.Getenv("PACK_DEFAULT"), os.Getenv("PACK_OVERRIDE"))
	os.Exit(23)
}
